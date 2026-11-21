"""FFmpeg encoders and the encoded-sample LiveKit transport."""

import ctypes as ct
from fractions import Fraction
from pathlib import Path

import av

from cinema.media import scale_audio

LIB = Path(__file__).resolve().parent


class VideoEncoder:
    def __init__(self, backend, width, height, fps, bitrate):
        api = self.api = ct.CDLL(str(LIB / "libcinema_encoder.so"))
        api.cinema_encoder_open.argtypes = [ct.c_char_p, ct.c_int, ct.c_int, ct.c_double, ct.c_int]
        api.cinema_encoder_open.restype = ct.c_void_p
        api.cinema_encode.argtypes = [ct.c_void_p, ct.c_char_p, ct.c_int, ct.c_int, ct.POINTER(ct.c_void_p)]
        api.cinema_encoder_free.argtypes = [ct.c_void_p]
        api.cinema_encoder_free.restype = None
        choices = {
            "software": ("libx264",),
            "vaapi": ("h264_vaapi", "libx264"),
            "nvenc": ("h264_nvenc", "libx264"),
        }
        for self.name in choices.get(backend, ("h264_vaapi", "h264_nvenc", "libx264")):
            self.handle = api.cinema_encoder_open(self.name.encode(), width, height, fps, bitrate)
            if self.handle:
                break
        else:
            raise RuntimeError("No usable FFmpeg H.264 encoder")

    def encode(self, data, keyframe=False):
        address = ct.c_void_p()
        size = self.api.cinema_encode(self.handle, data, len(data), keyframe, ct.byref(address))
        if size <= 0:
            raise RuntimeError("FFmpeg did not produce a video frame")
        return ct.string_at(address, size)

    def close(self):
        if self.handle:
            self.api.cinema_encoder_free(self.handle)
            self.handle = None


class OpusEncoder:
    def __init__(self):
        self.codec = av.CodecContext.create("libopus", "w")
        self.codec.sample_rate = 48000
        self.codec.layout = "stereo"
        self.codec.format = "s16"
        self.codec.bit_rate = 128000
        self.codec.time_base = Fraction(1, 48000)
        self.codec.options = {"frame_duration": "20", "application": "lowdelay"}
        self.codec.open()

    def encode(self, frame, volume):
        pcm = av.AudioFrame(format="s16", layout="stereo", samples=frame.samples)
        pcm.planes[0].update(scale_audio(frame.data, volume))
        pcm.sample_rate = 48000
        pcm.time_base = self.codec.time_base
        pcm.pts = round(frame.time * 48000)
        return self.codec.encode(pcm)

    def finish(self):
        return self.codec.encode(None)


class Publisher:
    def __init__(self):
        lib = self.lib = ct.CDLL(str(LIB / "libcinema_publisher.so"))
        lib.CinemaConnect.argtypes = [ct.c_char_p] * 3
        lib.CinemaTracks.argtypes = [ct.c_int, ct.c_int]
        lib.CinemaWrite.argtypes = [ct.c_int, ct.c_char_p, ct.c_int, ct.c_longlong, ct.c_longlong]
        lib.CinemaClose.restype = None

    def connect(self, grant):
        if self.lib.CinemaConnect(
            grant["endpoint"].encode(), grant["token"].encode(), grant.get("e2ee_key", "").encode()
        ):
            raise RuntimeError("Could not join the voice room")

    def tracks(self, width, height):
        if self.lib.CinemaTracks(width, height):
            raise RuntimeError("Could not publish screen-share tracks")

    def write(self, video, payload, duration, timestamp_ns):
        if self.lib.CinemaWrite(video, payload, len(payload), round(duration * 1e9), timestamp_ns):
            raise RuntimeError("Could not publish encoded media")

    def needs_keyframe(self):
        return bool(self.lib.CinemaNeedsKeyframe())

    def interrupted(self):
        return bool(self.lib.CinemaInterrupted())

    def close(self):
        self.lib.CinemaClose()
