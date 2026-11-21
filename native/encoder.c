/* FFmpeg owns format conversion, rate control, keyframes and device upload.
 * One encoder belongs to one playback generation. Output is Annex B H.264. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <libavcodec/avcodec.h>
#include <libavutil/hwcontext.h>
#include <libavutil/imgutils.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>

typedef struct {
    AVCodecContext *codec;
    AVFrame *frame;
    AVPacket *packet;
    struct SwsContext *scale;
    int64_t pts;
} Encoder;

void cinema_encoder_free(Encoder *e) {
    if (!e) return;
    avcodec_free_context(&e->codec);
    av_frame_free(&e->frame);
    av_packet_free(&e->packet);
    sws_freeContext(e->scale);
    free(e);
}

Encoder *cinema_encoder_open(const char *name, int w, int h, double fps, int bitrate) {
    const AVCodec *codec = avcodec_find_encoder_by_name(name);
    if (!codec) return NULL;
    Encoder *e = calloc(1, sizeof(*e));
    if (!e) return NULL;
    AVCodecContext *c = e->codec = avcodec_alloc_context3(codec);
    e->frame = av_frame_alloc();
    e->packet = av_packet_alloc();
    if (!c || !e->frame || !e->packet) goto fail;
    c->width = w;
    c->height = h;
    c->framerate = av_d2q(fps, 1001000);
    c->time_base = av_inv_q(c->framerate);
    c->bit_rate = c->rc_max_rate = bitrate;
    c->rc_buffer_size = bitrate;
    c->max_b_frames = 0;
    c->gop_size = (int)(fps + .5);
    c->thread_count = 2;
    c->flags |= AV_CODEC_FLAG_LOW_DELAY;
#ifdef AV_PROFILE_H264_CONSTRAINED_BASELINE
    c->profile = AV_PROFILE_H264_CONSTRAINED_BASELINE;
#else
    c->profile = FF_PROFILE_H264_CONSTRAINED_BASELINE;
#endif
    c->pix_fmt = AV_PIX_FMT_YUV420P;
    if (!strcmp(name, "h264_vaapi")) {
        if (av_hwdevice_ctx_create(&c->hw_device_ctx, AV_HWDEVICE_TYPE_VAAPI, NULL, NULL, 0) < 0) goto fail;
        c->hw_frames_ctx = av_hwframe_ctx_alloc(c->hw_device_ctx);
        if (!c->hw_frames_ctx) goto fail;
        AVHWFramesContext *pool = (AVHWFramesContext *)c->hw_frames_ctx->data;
        pool->format = c->pix_fmt = AV_PIX_FMT_VAAPI;
        pool->sw_format = AV_PIX_FMT_NV12;
        pool->width = w;
        pool->height = h;
        pool->initial_pool_size = 6;
        if (av_hwframe_ctx_init(c->hw_frames_ctx) < 0) goto fail;
        av_opt_set_int(c->priv_data, "async_depth", 1, 0);
        e->scale = sws_getContext(w,h,AV_PIX_FMT_YUV420P,w,h,AV_PIX_FMT_NV12,SWS_POINT,NULL,NULL,NULL);
        if (!e->scale) goto fail;
    } else if (!strcmp(name,"libx264")) {
        av_opt_set(c->priv_data,"preset","veryfast",0);
        av_opt_set(c->priv_data,"tune","zerolatency",0);
        av_opt_set(c->priv_data,"x264-params","repeat-headers=1:annexb=1",0);
    } else {
        av_opt_set(c->priv_data,"tune","ull",0);
        av_opt_set_int(c->priv_data,"zerolatency",1,0);
        av_opt_set_int(c->priv_data,"delay",0,0);
        av_opt_set_int(c->priv_data,"forced-idr",1,0);
    }
    if (avcodec_open2(c,codec,NULL) < 0) goto fail;
    e->frame->format = e->scale ? AV_PIX_FMT_NV12 : AV_PIX_FMT_YUV420P;
    e->frame->width = w;
    e->frame->height = h;
    if (av_frame_get_buffer(e->frame,32) < 0) goto fail;
    return e;
fail:
    cinema_encoder_free(e);
    return NULL;
}

// One non-reordered video access unit per input frame is required by the transport.
int cinema_encode(Encoder *e, const uint8_t *pixels, int length, int keyframe, const uint8_t **output) {
    int w=e->codec->width, h=e->codec->height;
    if (length != av_image_get_buffer_size(AV_PIX_FMT_YUV420P,w,h,1)) return -1;
    if (av_frame_make_writable(e->frame)<0) return -1;
    const uint8_t *planes[4]={pixels,pixels+w*h,pixels+w*h*5/4,NULL};
    const int strides[4]={w,w/2,w/2,0};
    if (e->scale) sws_scale(e->scale,planes,strides,0,h,e->frame->data,e->frame->linesize);
    else av_image_copy(e->frame->data,e->frame->linesize,planes,strides,AV_PIX_FMT_YUV420P,w,h);
    AVFrame *send=e->frame, *hw=NULL;
    if (e->scale) {
        hw=av_frame_alloc();
        if (!hw || av_hwframe_get_buffer(e->codec->hw_frames_ctx,hw,0)<0 ||
            av_hwframe_transfer_data(hw,e->frame,0)<0) { av_frame_free(&hw); return -1; }
        send=hw;
    }
    send->pts=e->pts++;
    send->pict_type=keyframe ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
    int res=avcodec_send_frame(e->codec,send);
    av_frame_free(&hw);
    if (res<0) return -1;
    av_packet_unref(e->packet);
    if (avcodec_receive_packet(e->codec,e->packet)<0) return -1;
    *output=e->packet->data;
    return e->packet->size;
}
