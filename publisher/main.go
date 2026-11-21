// The confined worker loads this library once. Each worker owns one room.
package main

import "C"
import (
	"bytes"
	"github.com/pion/webrtc/v4/pkg/media/h264reader"
	"io"
	"sync/atomic"
	"time"
	"unsafe"

	"github.com/livekit/protocol/livekit"
	lk "github.com/livekit/server-sdk-go/v2"
	"github.com/livekit/server-sdk-go/v2/e2ee"
	"github.com/pion/rtcp"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
)

var room *lk.Room
var key *e2ee.ExternalKeyProvider
var video, audio *lk.LocalTrack
var videoSID string
var width, height int
var interrupted atomic.Bool
var requestKeyFrame atomic.Bool

//export CinemaConnect
func CinemaConnect(endpoint, token, shared *C.char) C.int {
	if s := C.GoString(shared); s != "" {
		key = e2ee.NewExternalKeyProvider()
		if err := key.SetKeyFromPassphrase(s, 0); err != nil {
			return -1
		}
	}
	callback := lk.NewRoomCallback()
	callback.OnReconnecting = func() { interrupted.Store(true) }
	callback.OnDisconnected = func() { interrupted.Store(true) }
	room = lk.NewRoom(callback)
	if room.JoinWithToken(C.GoString(endpoint), C.GoString(token), lk.WithAutoSubscribe(false), lk.WithConnectTimeout(15*time.Second)) != nil {
		return -1
	}
	return 0
}

func publish(isVideo bool, w, h int) (*lk.LocalTrack, string, error) {
	codec := webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2}
	options := &lk.TrackPublicationOptions{Name: "cinema-audio", Source: livekit.TrackSource_SCREEN_SHARE_AUDIO, Stereo: true, DisableDTX: true}
	encryptionCodec := lk.CodecOpus
	if isVideo {
		codec = webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeH264, ClockRate: 90000, SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e028"}
		options = &lk.TrackPublicationOptions{Name: "cinema-video", Source: livekit.TrackSource_SCREEN_SHARE, VideoWidth: w, VideoHeight: h}
		encryptionCodec = lk.CodecH264
	}
	var opts []lk.LocalTrackOptions
	if isVideo {
		opts = append(opts, lk.WithRTCPHandler(func(packet rtcp.Packet) {
			switch packet.(type) {
			case *rtcp.PictureLossIndication, *rtcp.FullIntraRequest:
				requestKeyFrame.Store(true)
			}
		}))
	}
	if key != nil {
		enc, err := lk.NewFrameEncryptor(key, encryptionCodec)
		if err != nil {
			return nil, "", err
		}
		opts = append(opts, lk.WithFrameEncryptor(enc))
		options.Encryption = livekit.Encryption_GCM
	}
	track, err := lk.NewLocalTrack(codec, opts...)
	if err != nil {
		return nil, "", err
	}
	pub, err := room.LocalParticipant.PublishTrack(track, options)
	if err != nil {
		return nil, "", err
	}
	return track, pub.SID(), nil
}

//export CinemaTracks
func CinemaTracks(w, h C.int) C.int {
	if audio == nil {
		a, _, err := publish(false, 0, 0)
		if err != nil {
			return -1
		}
		audio = a
	}
	if video != nil && width == int(w) && height == int(h) {
		return 0
	}
	if video != nil {
		if room.LocalParticipant.UnpublishTrack(videoSID) != nil {
			return -1
		}
		video = nil
	}
	v, sid, err := publish(true, int(w), int(h))
	if err != nil {
		return -1
	}
	video, videoSID, width, height = v, sid, int(w), int(h)
	return 0
}

//export CinemaWrite
func CinemaWrite(isVideo C.int, data unsafe.Pointer, size C.int, duration, timestamp C.longlong) C.int {
	track := audio
	if isVideo != 0 {
		track = video
	}
	if track == nil {
		return -1
	}
	payload := C.GoBytes(data, size)
	if isVideo != 0 && key != nil {
		// Receivers reconstruct Annex B with four-byte start codes. Canonicalize
		// the authenticated header before passing it to LiveKit's frame encryptor.
		reader, err := h264reader.NewReader(bytes.NewReader(payload))
		if err != nil {
			return -1
		}
		var annex bytes.Buffer
		annex.Grow(len(payload))
		for {
			nal, err := reader.NextNAL()
			if err == io.EOF {
				break
			}
			if err != nil || len(nal.Data) == 0 {
				return -1
			}
			kind := nal.Data[0] & 31
			if kind == 9 || kind == 12 {
				continue
			}
			annex.Write([]byte{0, 0, 0, 1})
			annex.Write(nal.Data)
		}
		payload = annex.Bytes()
	}
	packet := media.Sample{Data: payload, Duration: time.Duration(duration), Timestamp: time.Unix(0, int64(timestamp))}
	if track.WriteSample(packet, nil) != nil {
		return -1
	}
	return 0
}

//export CinemaNeedsKeyframe
func CinemaNeedsKeyframe() C.int {
	if requestKeyFrame.Swap(false) {
		return 1
	}
	return 0
}

//export CinemaInterrupted
func CinemaInterrupted() C.int {
	if interrupted.Load() {
		return 1
	}
	return 0
}

//export CinemaClose
func CinemaClose() {
	if room != nil {
		room.Disconnect()
	}
}

func main() {}
