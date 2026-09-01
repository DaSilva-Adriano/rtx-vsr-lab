#define NOMINMAX
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef VSR_NGX_EXPORTS
#define VSR_NGX_EXPORTS
#endif

#include "vsr_ngx.h"

#include <d3d11_4.h>
#include <dxgi.h>

#include <nvsdk_ngx_defs.h>
#include <nvsdk_ngx_defs_truehdr.h>
#include <nvsdk_ngx_defs_vsr.h>
#include <nvsdk_ngx_helpers_truehdr.h>
#include <nvsdk_ngx_helpers_vsr.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "advapi32.lib")
#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "nvsdk_ngx_s.lib")

namespace {

constexpr unsigned long long kAppId = 0;
constexpr UINT kVendorNvidia = 0x10DE;
constexpr int kMaxOutW = 3840;
constexpr int kMaxOutH = 2160;

HMODULE g_self = nullptr;

template <class T>
void SafeRelease(T*& p)
{
    if (p) {
        p->Release();
        p = nullptr;
    }
}

void set_status(VsrStatus* st, int ok, int ngx, const char* msg)
{
    if (!st) return;
    st->ok = ok;
    st->ngx_result = ngx;
    st->message[0] = 0;
    if (msg) {
        strncpy_s(st->message, msg, _TRUNCATE);
    }
}

std::string wide_to_utf8(const wchar_t* w)
{
    if (!w || !w[0]) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 0) return {};
    std::string s(static_cast<size_t>(n - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, s.data(), n, nullptr, nullptr);
    return s;
}

bool file_exists(const std::wstring& p)
{
    DWORD a = GetFileAttributesW(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

std::wstring join_path(const std::wstring& a, const wchar_t* b)
{
    if (a.empty()) return b;
    if (a.back() == L'\\' || a.back() == L'/') return a + b;
    return a + L"\\" + b;
}

std::wstring module_dir()
{
    wchar_t buf[MAX_PATH] = {};
    HMODULE h = g_self ? g_self : GetModuleHandleW(nullptr);
    GetModuleFileNameW(h, buf, MAX_PATH);
    std::wstring p(buf);
    size_t slash = p.find_last_of(L"\\/");
    if (slash == std::wstring::npos) return L".";
    return p.substr(0, slash);
}

std::wstring local_app_data()
{
    wchar_t buf[MAX_PATH] = {};
    DWORD n = GetEnvironmentVariableW(L"LOCALAPPDATA", buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return L".";
    std::wstring dir = join_path(buf, L"RTX_VSR_Lab");
    CreateDirectoryW(dir.c_str(), nullptr);
    return dir;
}

void copy_cstr(char* dst, size_t dst_sz, const std::string& s)
{
    if (!dst || dst_sz == 0) return;
    strncpy_s(dst, dst_sz, s.c_str(), _TRUNCATE);
}

const char* ngx_name(int r)
{
    switch (static_cast<unsigned int>(r)) {
    case NVSDK_NGX_Result_Success: return "Success";
    case NVSDK_NGX_Result_Fail: return "Fail";
    case NVSDK_NGX_Result_FAIL_FeatureNotSupported: return "FAIL_FeatureNotSupported";
    case NVSDK_NGX_Result_FAIL_PlatformError: return "FAIL_PlatformError";
    case NVSDK_NGX_Result_FAIL_FeatureAlreadyExists: return "FAIL_FeatureAlreadyExists";
    case NVSDK_NGX_Result_FAIL_FeatureNotFound: return "FAIL_FeatureNotFound";
    case NVSDK_NGX_Result_FAIL_InvalidParameter: return "FAIL_InvalidParameter";
    case NVSDK_NGX_Result_FAIL_ScratchBufferTooSmall: return "FAIL_ScratchBufferTooSmall";
    case NVSDK_NGX_Result_FAIL_NotInitialized: return "FAIL_NotInitialized";
    case NVSDK_NGX_Result_FAIL_UnsupportedInputFormat: return "FAIL_UnsupportedInputFormat";
    case NVSDK_NGX_Result_FAIL_RWFlagMissing: return "FAIL_RWFlagMissing";
    case NVSDK_NGX_Result_FAIL_MissingInput: return "FAIL_MissingInput";
    case NVSDK_NGX_Result_FAIL_UnableToInitializeFeature: return "FAIL_UnableToInitializeFeature";
    case NVSDK_NGX_Result_FAIL_OutOfDate: return "FAIL_OutOfDate";
    case NVSDK_NGX_Result_FAIL_OutOfGPUMemory: return "FAIL_OutOfGPUMemory";
    case NVSDK_NGX_Result_FAIL_UnsupportedFormat: return "FAIL_UnsupportedFormat";
    case NVSDK_NGX_Result_FAIL_UnableToWriteToAppDataPath: return "FAIL_UnableToWriteToAppDataPath";
    case NVSDK_NGX_Result_FAIL_UnsupportedParameter: return "FAIL_UnsupportedParameter";
    case NVSDK_NGX_Result_FAIL_Denied: return "FAIL_Denied";
    case NVSDK_NGX_Result_FAIL_NotImplemented: return "FAIL_NotImplemented";
    default: return "Unknown";
    }
}

void ngx_fail(VsrStatus* st, NVSDK_NGX_Result r, const char* where)
{
    char buf[512];
    sprintf_s(buf, "%s: %s (0x%08X)", where, ngx_name(static_cast<int>(r)), static_cast<unsigned>(r));
    set_status(st, 0, static_cast<int>(r), buf);
}

class Engine {
public:
    std::mutex mutex;
    ID3D11Device* device = nullptr;
    ID3D11DeviceContext* ctx = nullptr;
    ID3D10Multithread* mt = nullptr;

    NVSDK_NGX_Parameter* params = nullptr;
    NVSDK_NGX_Handle* vsr = nullptr;
    NVSDK_NGX_Handle* thdr = nullptr;
    bool ngx_inited = false;

    ID3D11Texture2D* texIn = nullptr;
    ID3D11Texture2D* texOut = nullptr;
    ID3D11Texture2D* texMid = nullptr;
    ID3D11Texture2D* texCanvas = nullptr;
    ID3D11Texture2D* stageIn = nullptr;
    ID3D11Texture2D* stageOut = nullptr;
    ID3D11RenderTargetView* rtvOut = nullptr;
    ID3D11RenderTargetView* rtvCanvas = nullptr;

    int texInW = 0, texInH = 0;
    int texOutW = 0, texOutH = 0;
    int texMidW = 0, texMidH = 0;
    int texCanvasW = 0, texCanvasH = 0;
    DXGI_FORMAT texOutFmt = DXGI_FORMAT_R8G8B8A8_UNORM;

    std::vector<std::wstring> pathStore;
    std::vector<const wchar_t*> pathPtrs;
    NVSDK_NGX_FeatureCommonInfo featureInfo{};
    std::wstring appData;

    VsrInfo info{};

    ~Engine() { shutdown(); }

    void enter()
    {
        if (mt) mt->Enter();
    }
    void leave()
    {
        if (mt) mt->Leave();
    }

    HRESULT create_tex(int w, int h, DXGI_FORMAT fmt, D3D11_USAGE usage, UINT bind, UINT cpu,
                       ID3D11Texture2D** out)
    {
        D3D11_TEXTURE2D_DESC d{};
        d.Width = static_cast<UINT>(w);
        d.Height = static_cast<UINT>(h);
        d.MipLevels = 1;
        d.ArraySize = 1;
        d.Format = fmt;
        d.SampleDesc.Count = 1;
        d.Usage = usage;
        d.BindFlags = bind;
        d.CPUAccessFlags = cpu;
        return device->CreateTexture2D(&d, nullptr, out);
    }

    void release_io()
    {
        SafeRelease(rtvOut);
        SafeRelease(rtvCanvas);
        SafeRelease(texIn);
        SafeRelease(texOut);
        SafeRelease(texMid);
        SafeRelease(texCanvas);
        SafeRelease(stageIn);
        SafeRelease(stageOut);
        texInW = texInH = texOutW = texOutH = 0;
        texMidW = texMidH = texCanvasW = texCanvasH = 0;
    }

    bool ensure_in(int w, int h, VsrStatus* st)
    {
        if (texIn && stageIn && texInW == w && texInH == h) return true;
        SafeRelease(texIn);
        SafeRelease(stageIn);
        texInW = texInH = 0;
        HRESULT hr = create_tex(w, h, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_USAGE_DEFAULT,
                                D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET | D3D11_BIND_UNORDERED_ACCESS,
                                0, &texIn);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateTexture2D input failed");
            return false;
        }
        hr = create_tex(w, h, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_USAGE_STAGING, 0,
                        D3D11_CPU_ACCESS_WRITE, &stageIn);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateTexture2D input staging failed");
            return false;
        }
        texInW = w;
        texInH = h;
        return true;
    }

    bool ensure_out(int w, int h, DXGI_FORMAT fmt, VsrStatus* st)
    {
        if (texOut && stageOut && rtvOut && texOutW == w && texOutH == h && texOutFmt == fmt) return true;
        SafeRelease(rtvOut);
        SafeRelease(texOut);
        SafeRelease(stageOut);
        texOutW = texOutH = 0;
        HRESULT hr = create_tex(w, h, fmt, D3D11_USAGE_DEFAULT,
                                D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET | D3D11_BIND_UNORDERED_ACCESS,
                                0, &texOut);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateTexture2D output failed");
            return false;
        }
        hr = create_tex(w, h, fmt, D3D11_USAGE_STAGING, 0, D3D11_CPU_ACCESS_READ, &stageOut);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateTexture2D output staging failed");
            return false;
        }
        hr = device->CreateRenderTargetView(texOut, nullptr, &rtvOut);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateRenderTargetView output failed");
            return false;
        }
        texOutW = w;
        texOutH = h;
        texOutFmt = fmt;
        return true;
    }

    bool ensure_mid(int w, int h, VsrStatus* st)
    {
        if (texMid && texMidW == w && texMidH == h) return true;
        SafeRelease(texMid);
        texMidW = texMidH = 0;
        HRESULT hr = create_tex(w, h, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_USAGE_DEFAULT,
                                D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET | D3D11_BIND_UNORDERED_ACCESS,
                                0, &texMid);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateTexture2D middle (VSR->TrueHDR) failed");
            return false;
        }
        texMidW = w;
        texMidH = h;
        return true;
    }

    bool ensure_canvas(int w, int h, VsrStatus* st)
    {
        if (texCanvas && rtvCanvas && texCanvasW == w && texCanvasH == h) return true;
        SafeRelease(rtvCanvas);
        SafeRelease(texCanvas);
        texCanvasW = texCanvasH = 0;
        HRESULT hr = create_tex(w, h, DXGI_FORMAT_R8G8B8A8_UNORM, D3D11_USAGE_DEFAULT,
                                D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET | D3D11_BIND_UNORDERED_ACCESS,
                                0, &texCanvas);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateTexture2D canvas failed");
            return false;
        }
        hr = device->CreateRenderTargetView(texCanvas, nullptr, &rtvCanvas);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateRenderTargetView canvas failed");
            return false;
        }
        texCanvasW = w;
        texCanvasH = h;
        return true;
    }

    bool upload_rgba(const unsigned char* src, int w, int h, VsrStatus* st)
    {
        D3D11_MAPPED_SUBRESOURCE mapped{};
        HRESULT hr = ctx->Map(stageIn, 0, D3D11_MAP_WRITE, 0, &mapped);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "Map input staging failed");
            return false;
        }
        const int row = w * 4;
        for (int y = 0; y < h; ++y) {
            memcpy(static_cast<unsigned char*>(mapped.pData) + static_cast<size_t>(y) * mapped.RowPitch,
                   src + static_cast<size_t>(y) * row, static_cast<size_t>(row));
        }
        ctx->Unmap(stageIn, 0);
        ctx->CopyResource(texIn, stageIn);
        return true;
    }

    bool download_rgba8(ID3D11Texture2D* gpu, ID3D11Texture2D* staging, unsigned char* dst, int w, int h,
                        VsrStatus* st)
    {
        ctx->CopyResource(staging, gpu);
        D3D11_MAPPED_SUBRESOURCE mapped{};
        HRESULT hr = ctx->Map(staging, 0, D3D11_MAP_READ, 0, &mapped);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "Map output staging failed");
            return false;
        }
        const int row = w * 4;
        for (int y = 0; y < h; ++y) {
            memcpy(dst + static_cast<size_t>(y) * row,
                   static_cast<unsigned char*>(mapped.pData) + static_cast<size_t>(y) * mapped.RowPitch,
                   static_cast<size_t>(row));
        }
        ctx->Unmap(staging, 0);
        return true;
    }

    bool download_r10_as_rgba8(unsigned char* dst, int w, int h, VsrStatus* st)
    {
        ctx->CopyResource(stageOut, texOut);
        D3D11_MAPPED_SUBRESOURCE mapped{};
        HRESULT hr = ctx->Map(stageOut, 0, D3D11_MAP_READ, 0, &mapped);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "Map TrueHDR staging failed");
            return false;
        }
        for (int y = 0; y < h; ++y) {
            const uint32_t* src = reinterpret_cast<const uint32_t*>(
                static_cast<unsigned char*>(mapped.pData) + static_cast<size_t>(y) * mapped.RowPitch);
            unsigned char* row = dst + static_cast<size_t>(y) * w * 4;
            for (int x = 0; x < w; ++x) {
                uint32_t p = src[x];
                row[x * 4 + 0] = static_cast<unsigned char>(((p >> 0) & 1023) >> 2);
                row[x * 4 + 1] = static_cast<unsigned char>(((p >> 10) & 1023) >> 2);
                row[x * 4 + 2] = static_cast<unsigned char>(((p >> 20) & 1023) >> 2);
                row[x * 4 + 3] = static_cast<unsigned char>(((p >> 30) & 3) * 85);
            }
        }
        ctx->Unmap(stageOut, 0);
        return true;
    }

    NVSDK_NGX_Result eval_vsr(ID3D11Resource* inRes, ID3D11Resource* outRes, int inW, int inH,
                              int outX, int outY, int outW, int outH, int quality)
    {
        NVSDK_NGX_D3D11_VSR_Eval_Params p{};
        p.pInput = inRes;
        p.pOutput = outRes;
        p.InputSubrectBase.X = 0;
        p.InputSubrectBase.Y = 0;
        p.InputSubrectSize.Width = static_cast<unsigned>(inW);
        p.InputSubrectSize.Height = static_cast<unsigned>(inH);
        p.OutputSubrectBase.X = static_cast<unsigned>(outX);
        p.OutputSubrectBase.Y = static_cast<unsigned>(outY);
        p.OutputSubrectSize.Width = static_cast<unsigned>(outW);
        p.OutputSubrectSize.Height = static_cast<unsigned>(outH);
        p.QualityLevel = static_cast<NVSDK_NGX_VSR_QualityLevel>(quality);
        enter();
        NVSDK_NGX_Result r = NGX_D3D11_EVALUATE_VSR_EXT(ctx, vsr, params, &p);
        leave();
        return r;
    }

    NVSDK_NGX_Result eval_thdr(ID3D11Resource* inRes, ID3D11Resource* outRes, int x, int y, int w, int h)
    {
        NVSDK_NGX_D3D11_TRUEHDR_Eval_Params p{};
        p.pInput = inRes;
        p.pOutput = outRes;
        p.InputSubrectTL.X = static_cast<unsigned>(x);
        p.InputSubrectTL.Y = static_cast<unsigned>(y);
        p.InputSubrectBR.Width = static_cast<unsigned>(x + w);
        p.InputSubrectBR.Height = static_cast<unsigned>(y + h);
        p.OutputSubrectTL.X = static_cast<unsigned>(x);
        p.OutputSubrectTL.Y = static_cast<unsigned>(y);
        p.OutputSubrectBR.Width = static_cast<unsigned>(x + w);
        p.OutputSubrectBR.Height = static_cast<unsigned>(y + h);
        p.Contrast = 100;
        p.Saturation = 100;
        p.MiddleGray = 50;
        p.MaxLuminance = 1000;
        enter();
        NVSDK_NGX_Result r = NGX_D3D11_EVALUATE_TRUEHDR_EXT(ctx, thdr, params, &p);
        leave();
        return r;
    }

    int init(const wchar_t* extra_dll_dir, VsrStatus* st)
    {
        shutdown();
        memset(&info, 0, sizeof(info));

        HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        (void)hr;

        IDXGIFactory1* factory = nullptr;
        hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), reinterpret_cast<void**>(&factory));
        if (FAILED(hr)) {
            set_status(st, 0, 0, "CreateDXGIFactory1 failed");
            return 0;
        }

        IDXGIAdapter1* chosen = nullptr;
        IDXGIAdapter1* ad = nullptr;
        for (UINT i = 0; factory->EnumAdapters1(i, &ad) != DXGI_ERROR_NOT_FOUND; ++i) {
            DXGI_ADAPTER_DESC1 desc{};
            ad->GetDesc1(&desc);
            if (!(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) && desc.VendorId == kVendorNvidia) {
                if (!chosen) {
                    chosen = ad;
                    ad = nullptr;
                    copy_cstr(info.gpu_name, sizeof(info.gpu_name), wide_to_utf8(desc.Description));
                }
            }
            SafeRelease(ad);
        }
        SafeRelease(factory);
        if (!chosen) {
            set_status(st, 0, 0, "No NVIDIA DXGI adapter found");
            return 0;
        }

        D3D_FEATURE_LEVEL fl_in[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0};
        D3D_FEATURE_LEVEL fl{};
        hr = D3D11CreateDevice(chosen, D3D_DRIVER_TYPE_UNKNOWN, nullptr, 0, fl_in, 2,
                               D3D11_SDK_VERSION, &device, &fl, &ctx);
        SafeRelease(chosen);
        if (FAILED(hr)) {
            set_status(st, 0, 0, "D3D11CreateDevice failed on NVIDIA adapter");
            return 0;
        }

        if (SUCCEEDED(ctx->QueryInterface(__uuidof(ID3D10Multithread), reinterpret_cast<void**>(&mt)))) {
            mt->SetMultithreadProtected(TRUE);
        }

        pathStore.clear();
        auto add_dir = [&](const std::wstring& d) {
            if (d.empty()) return;
            for (const auto& e : pathStore) {
                if (_wcsicmp(e.c_str(), d.c_str()) == 0) return;
            }
            pathStore.push_back(d);
        };

        if (extra_dll_dir && extra_dll_dir[0]) add_dir(extra_dll_dir);
        add_dir(module_dir());
        add_dir(L"C:\\VSR");
        add_dir(L"C:\\VSR\\RTX_Video_SDK_v1.1.0\\bin\\Windows\\x64\\rel");
        add_dir(L"C:\\VSR\\RTX_Video_SDK_v1.1.0\\bin\\Windows\\x64\\dev");

        pathPtrs.clear();
        for (auto& p : pathStore) pathPtrs.push_back(p.c_str());

        memset(&featureInfo, 0, sizeof(featureInfo));
        featureInfo.PathListInfo.Path = pathPtrs.data();
        featureInfo.PathListInfo.Length = static_cast<unsigned>(pathPtrs.size());
        featureInfo.LoggingInfo.LoggingCallback = [](const char*, NVSDK_NGX_Logging_Level, NVSDK_NGX_Feature) {};
        featureInfo.LoggingInfo.MinimumLoggingLevel = NVSDK_NGX_LOGGING_LEVEL_OFF;
        featureInfo.LoggingInfo.DisableOtherLoggingSinks = true;

        appData = local_app_data();

        std::wstring found_vsr;
        std::wstring found_thdr;
        for (const auto& d : pathStore) {
            auto v = join_path(d, L"nvngx_vsr.dll");
            auto t = join_path(d, L"nvngx_truehdr.dll");
            if (found_vsr.empty() && file_exists(v)) found_vsr = v;
            if (found_thdr.empty() && file_exists(t)) found_thdr = t;
        }
        copy_cstr(info.dll_dir, sizeof(info.dll_dir), wide_to_utf8(pathStore.empty() ? L"" : pathStore[0].c_str()));
        copy_cstr(info.vsr_dll, sizeof(info.vsr_dll), wide_to_utf8(found_vsr.c_str()));
        copy_cstr(info.truehdr_dll, sizeof(info.truehdr_dll), wide_to_utf8(found_thdr.c_str()));

        if (found_vsr.empty()) {
            set_status(st, 0, 0, "nvngx_vsr.dll not found. Put it next to the exe or in C:\\VSR");
            return 0;
        }

        NVSDK_NGX_Result ngx = NVSDK_NGX_D3D11_Init(kAppId, appData.c_str(), device, &featureInfo);
        if (NVSDK_NGX_FAILED(ngx)) {
            ngx_fail(st, ngx, "NVSDK_NGX_D3D11_Init");
            return 0;
        }
        ngx_inited = true;

        ngx = NVSDK_NGX_D3D11_GetCapabilityParameters(&params);
        if (NVSDK_NGX_FAILED(ngx) || !params) {
            ngx_fail(st, ngx, "GetCapabilityParameters");
            return 0;
        }

        int vsr_avail = 0, thdr_avail = 0, need_drv = 0, maj = 0, minr = 0, init_res = 0;
        params->Get(NVSDK_NGX_Parameter_VSR_Available, &vsr_avail);
        params->Get(NVSDK_NGX_Parameter_TrueHDR_Available, &thdr_avail);
        params->Get(NVSDK_NGX_Parameter_VSR_NeedsUpdatedDriver, &need_drv);
        params->Get(NVSDK_NGX_Parameter_VSR_MinDriverVersionMajor, &maj);
        params->Get(NVSDK_NGX_Parameter_VSR_MinDriverVersionMinor, &minr);
        params->Get(NVSDK_NGX_Parameter_VSR_FeatureInitResult, &init_res);
        info.vsr_available = vsr_avail ? 1 : 0;
        info.truehdr_available = thdr_avail ? 1 : 0;
        info.needs_updated_driver = need_drv ? 1 : 0;
        info.min_driver_major = maj;
        info.min_driver_minor = minr;
        info.feature_init_result = init_res;

        if (!vsr_avail) {
            set_status(st, 0, init_res, "VSR not available on this GPU/driver (VSR.Available=0)");
            return 0;
        }

        enter();
        size_t scratch = 0;
        ngx = NVSDK_NGX_D3D11_GetScratchBufferSize(NVSDK_NGX_Feature_VSR, params, &scratch);
        if (NVSDK_NGX_FAILED(ngx)) {
            leave();
            ngx_fail(st, ngx, "GetScratchBufferSize VSR");
            return 0;
        }
        NVSDK_NGX_Feature_Create_Params create{};
        ngx = NGX_D3D11_CREATE_VSR_EXT(ctx, &vsr, params, &create);
        leave();
        if (NVSDK_NGX_FAILED(ngx) || !vsr) {
            ngx_fail(st, ngx, "CREATE_VSR");
            return 0;
        }

        if (thdr_avail && !found_thdr.empty()) {
            enter();
            scratch = 0;
            ngx = NVSDK_NGX_D3D11_GetScratchBufferSize(NVSDK_NGX_Feature_TrueHDR, params, &scratch);
            if (NVSDK_NGX_SUCCEED(ngx)) {
                NVSDK_NGX_Feature_Create_Params thcreate{};
                ngx = NGX_D3D11_CREATE_TRUEHDR_EXT(ctx, &thdr, params, &thcreate);
                if (NVSDK_NGX_FAILED(ngx)) {
                    thdr = nullptr;
                    info.truehdr_available = 0;
                }
            } else {
                info.truehdr_available = 0;
            }
            leave();
        } else {
            info.truehdr_available = 0;
        }

        set_status(st, 1, NVSDK_NGX_Result_Success, "NGX VSR ready");
        return 1;
    }

    void shutdown()
    {
        release_io();
        if (ngx_inited) {
            if (vsr) {
                NVSDK_NGX_D3D11_ReleaseFeature(vsr);
                vsr = nullptr;
            }
            if (thdr) {
                NVSDK_NGX_D3D11_ReleaseFeature(thdr);
                thdr = nullptr;
            }
            if (params) {
                NVSDK_NGX_D3D11_DestroyParameters(params);
                params = nullptr;
            }
            NVSDK_NGX_D3D11_Shutdown1(device);
            ngx_inited = false;
        }
        SafeRelease(mt);
        SafeRelease(ctx);
        SafeRelease(device);
        memset(&info, 0, sizeof(info));
    }

    int try_size(int in_w, int in_h, int out_w, int out_h, int quality, int out_x, int out_y,
                 int content_w, int content_h, VsrStatus* st)
    {
        if (!vsr) {
            set_status(st, 0, 0, "VSR not initialized");
            return 0;
        }
        if (in_w < 1 || in_h < 1 || out_w < 1 || out_h < 1) {
            set_status(st, 0, NVSDK_NGX_Result_FAIL_InvalidParameter, "invalid size");
            return 0;
        }
        if (content_w <= 0) content_w = out_w;
        if (content_h <= 0) content_h = out_h;

        if (!ensure_in(in_w, in_h, st)) return 0;
        if (!ensure_out(out_w, out_h, DXGI_FORMAT_R8G8B8A8_UNORM, st)) return 0;

        const FLOAT black[4] = {0, 0, 0, 1};
        ctx->ClearRenderTargetView(rtvOut, black);

        NVSDK_NGX_Result ngx = eval_vsr(texIn, texOut, in_w, in_h, out_x, out_y, content_w, content_h, quality);
        if (NVSDK_NGX_FAILED(ngx)) {
            ngx_fail(st, ngx, "Evaluate VSR");
            return 0;
        }
        set_status(st, 1, static_cast<int>(ngx), "Evaluate VSR ok");
        return 1;
    }

    int evaluate(const unsigned char* rgba_in, unsigned char* rgba_out, const VsrEvalDesc* desc, VsrStatus* st)
    {
        if (!vsr || !desc || !rgba_in || !rgba_out) {
            set_status(st, 0, 0, "evaluate: null argument or VSR not initialized");
            return 0;
        }
        const int in_w = desc->in_w;
        const int in_h = desc->in_h;
        const int out_w = desc->out_w;
        const int out_h = desc->out_h;
        int cx = desc->content_x;
        int cy = desc->content_y;
        int cw = desc->content_w > 0 ? desc->content_w : out_w;
        int ch = desc->content_h > 0 ? desc->content_h : out_h;
        const int quality = desc->quality;
        const int want_thdr = desc->truehdr ? 1 : 0;

        if (want_thdr && !thdr) {
            set_status(st, 0, 0, "TrueHDR requested but feature is not available");
            return 0;
        }
        if (quality < 1 || quality > 4) {
            set_status(st, 0, NVSDK_NGX_Result_FAIL_InvalidParameter, "quality must be 1-4 (0 is bicubic, not VSR)");
            return 0;
        }
        if (out_w > kMaxOutW || out_h > kMaxOutH) {
            set_status(st, 0, NVSDK_NGX_Result_FAIL_InvalidParameter, "output exceeds 3840x2160");
            return 0;
        }
        if (cw > out_w || ch > out_h || cx < 0 || cy < 0 || cx + cw > out_w || cy + ch > out_h) {
            set_status(st, 0, NVSDK_NGX_Result_FAIL_InvalidParameter, "content rect outside output canvas");
            return 0;
        }

        const DXGI_FORMAT outFmt = want_thdr ? DXGI_FORMAT_R10G10B10A2_UNORM : DXGI_FORMAT_R8G8B8A8_UNORM;
        if (!ensure_in(in_w, in_h, st)) return 0;
        if (!upload_rgba(rgba_in, in_w, in_h, st)) return 0;

        const bool letterbox = (cx != 0 || cy != 0 || cw != out_w || ch != out_h);

        auto run_vsr = [&](ID3D11Resource* dst, int ox, int oy, int ow, int oh) -> NVSDK_NGX_Result {
            return eval_vsr(texIn, dst, in_w, in_h, ox, oy, ow, oh, quality);
        };

        NVSDK_NGX_Result ngx = NVSDK_NGX_Result_Fail;
        ID3D11Texture2D* vsrDst = nullptr;
        int vsrOx = 0, vsrOy = 0;

        if (!letterbox) {
            if (want_thdr) {
                if (!ensure_mid(out_w, out_h, st)) return 0;
                if (!ensure_out(out_w, out_h, outFmt, st)) return 0;
                ngx = run_vsr(texMid, 0, 0, out_w, out_h);
                vsrDst = texMid;
            } else {
                if (!ensure_out(out_w, out_h, outFmt, st)) return 0;
                ngx = run_vsr(texOut, 0, 0, out_w, out_h);
                vsrDst = texOut;
            }
        } else {
            // Prefer VSR into a content-sized surface (exact input/output surfaces),
            // then blit onto the user canvas. One VSR pass, no extra scale.
            if (want_thdr) {
                if (!ensure_mid(cw, ch, st)) return 0;
                ngx = run_vsr(texMid, 0, 0, cw, ch);
                vsrDst = texMid;
            } else {
                if (!ensure_out(cw, ch, DXGI_FORMAT_R8G8B8A8_UNORM, st)) return 0;
                ngx = run_vsr(texOut, 0, 0, cw, ch);
                vsrDst = texOut;
            }
            vsrOx = 0;
            vsrOy = 0;
        }

        if (NVSDK_NGX_FAILED(ngx)) {
            ngx_fail(st, ngx, "Evaluate VSR");
            return 0;
        }

        if (want_thdr) {
            if (!ensure_out(letterbox ? cw : out_w, letterbox ? ch : out_h, outFmt, st)) return 0;
            ngx = eval_thdr(vsrDst, texOut, vsrOx, vsrOy, cw, ch);
            if (NVSDK_NGX_FAILED(ngx)) {
                ngx_fail(st, ngx, "Evaluate TrueHDR");
                return 0;
            }
        }

        if (!letterbox) {
            if (want_thdr) {
                if (!download_r10_as_rgba8(rgba_out, out_w, out_h, st)) return 0;
            } else {
                if (!download_rgba8(texOut, stageOut, rgba_out, out_w, out_h, st)) return 0;
            }
            set_status(st, 1, NVSDK_NGX_Result_Success, "Evaluate ok");
            return 1;
        }

        std::vector<unsigned char> content(static_cast<size_t>(cw) * ch * 4);
        if (want_thdr) {
            if (!download_r10_as_rgba8(content.data(), cw, ch, st)) return 0;
        } else {
            if (!download_rgba8(texOut, stageOut, content.data(), cw, ch, st)) return 0;
        }
        memset(rgba_out, 0, static_cast<size_t>(out_w) * out_h * 4);
        for (int y = 0; y < ch; ++y) {
            memcpy(rgba_out + (static_cast<size_t>(cy + y) * out_w + cx) * 4,
                   content.data() + static_cast<size_t>(y) * cw * 4,
                   static_cast<size_t>(cw) * 4);
        }
        set_status(st, 1, NVSDK_NGX_Result_Success, "Evaluate ok (letterbox pad after one VSR pass)");
        return 1;
    }
};

Engine g_engine;

int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

} // namespace

BOOL APIENTRY DllMain(HMODULE h, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = h;
        DisableThreadLibraryCalls(h);
    }
    return TRUE;
}

int vsr_init(const wchar_t* extra_dll_dir, VsrStatus* status)
{
    std::lock_guard<std::mutex> lock(g_engine.mutex);
    return g_engine.init(extra_dll_dir, status);
}

void vsr_shutdown(void)
{
    std::lock_guard<std::mutex> lock(g_engine.mutex);
    g_engine.shutdown();
}

int vsr_get_info(VsrInfo* info)
{
    std::lock_guard<std::mutex> lock(g_engine.mutex);
    if (!info) return 0;
    *info = g_engine.info;
    return g_engine.vsr ? 1 : 0;
}

int vsr_try_size(int in_w, int in_h, int out_w, int out_h, int quality, int out_x, int out_y,
                 int content_w, int content_h, VsrStatus* status)
{
    std::lock_guard<std::mutex> lock(g_engine.mutex);
    return g_engine.try_size(in_w, in_h, out_w, out_h, quality, out_x, out_y, content_w, content_h, status);
}

int vsr_evaluate(const unsigned char* rgba_in, unsigned char* rgba_out, const VsrEvalDesc* desc,
                 VsrStatus* status)
{
    std::lock_guard<std::mutex> lock(g_engine.mutex);
    return g_engine.evaluate(rgba_in, rgba_out, desc, status);
}

int vsr_resize_rgba(const unsigned char* src, int sw, int sh, unsigned char* dst, int dw, int dh, int filter)
{
    if (!src || !dst || sw < 1 || sh < 1 || dw < 1 || dh < 1) return 0;
    if (sw == dw && sh == dh) {
        memcpy(dst, src, static_cast<size_t>(sw) * sh * 4);
        return 1;
    }
    if (filter == 0) {
        for (int y = 0; y < dh; ++y) {
            int sy = clampi(y * sh / dh, 0, sh - 1);
            const unsigned char* srow = src + static_cast<size_t>(sy) * sw * 4;
            unsigned char* drow = dst + static_cast<size_t>(y) * dw * 4;
            for (int x = 0; x < dw; ++x) {
                int sx = clampi(x * sw / dw, 0, sw - 1);
                memcpy(drow + x * 4, srow + sx * 4, 4);
            }
        }
        return 1;
    }
    const float xscale = static_cast<float>(sw) / static_cast<float>(dw);
    const float yscale = static_cast<float>(sh) / static_cast<float>(dh);
    for (int y = 0; y < dh; ++y) {
        float fy = (static_cast<float>(y) + 0.5f) * yscale - 0.5f;
        int y0 = clampi(static_cast<int>(std::floor(fy)), 0, sh - 1);
        int y1 = clampi(y0 + 1, 0, sh - 1);
        float ty = fy - std::floor(fy);
        unsigned char* drow = dst + static_cast<size_t>(y) * dw * 4;
        for (int x = 0; x < dw; ++x) {
            float fx = (static_cast<float>(x) + 0.5f) * xscale - 0.5f;
            int x0 = clampi(static_cast<int>(std::floor(fx)), 0, sw - 1);
            int x1 = clampi(x0 + 1, 0, sw - 1);
            float tx = fx - std::floor(fx);
            const unsigned char* p00 = src + (static_cast<size_t>(y0) * sw + x0) * 4;
            const unsigned char* p10 = src + (static_cast<size_t>(y0) * sw + x1) * 4;
            const unsigned char* p01 = src + (static_cast<size_t>(y1) * sw + x0) * 4;
            const unsigned char* p11 = src + (static_cast<size_t>(y1) * sw + x1) * 4;
            for (int c = 0; c < 4; ++c) {
                float a = p00[c] + (p10[c] - p00[c]) * tx;
                float b = p01[c] + (p11[c] - p01[c]) * tx;
                float v = a + (b - a) * ty;
                drow[x * 4 + c] = static_cast<unsigned char>(clampi(static_cast<int>(v + 0.5f), 0, 255));
            }
        }
    }
    return 1;
}

const char* vsr_ngx_name(int ngx_result)
{
    return ngx_name(ngx_result);
}
