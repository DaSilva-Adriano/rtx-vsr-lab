#pragma once

#include <stdint.h>

#ifdef _WIN32
#ifdef VSR_NGX_EXPORTS
#define VSR_API __declspec(dllexport)
#else
#define VSR_API __declspec(dllimport)
#endif
#else
#define VSR_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VsrStatus {
    int ok;
    int ngx_result;
    char message[512];
} VsrStatus;

typedef struct VsrInfo {
    int vsr_available;
    int truehdr_available;
    int needs_updated_driver;
    int min_driver_major;
    int min_driver_minor;
    int feature_init_result;
    char gpu_name[256];
    char dll_dir[512];
    char vsr_dll[512];
    char truehdr_dll[512];
} VsrInfo;

typedef struct VsrEvalDesc {
    int in_w;
    int in_h;
    int out_w;
    int out_h;
    int content_x;
    int content_y;
    int content_w;
    int content_h;
    int quality;
    int truehdr;
} VsrEvalDesc;

VSR_API int vsr_init(const wchar_t* extra_dll_dir, VsrStatus* status);
VSR_API void vsr_shutdown(void);
VSR_API int vsr_get_info(VsrInfo* info);
VSR_API int vsr_try_size(int in_w, int in_h, int out_w, int out_h, int quality,
                         int out_x, int out_y, int content_w, int content_h,
                         VsrStatus* status);
VSR_API int vsr_evaluate(const unsigned char* rgba_in, unsigned char* rgba_out,
                         const VsrEvalDesc* desc, VsrStatus* status);
VSR_API int vsr_resize_rgba(const unsigned char* src, int sw, int sh,
                            unsigned char* dst, int dw, int dh, int filter);
VSR_API const char* vsr_ngx_name(int ngx_result);

#ifdef __cplusplus
}
#endif
