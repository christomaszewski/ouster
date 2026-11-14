#ifndef OUSTER_GPU_CLOUD_NATIVE_BACKEND_H
#define OUSTER_GPU_CLOUD_NATIVE_BACKEND_H
#include <stddef.h>
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
// Experimental C ABI: only single-return LEGACY, original 48-byte organized
// output, destagger enabled, all rows and no mask. No ROS or SDK ABI crosses it.
#define OUSTER_GPU_ABI_VERSION 1u
enum ouster_gpu_ownership {
    OUSTER_GPU_STAGED = 0,             // native input copy + final host copy
    OUSTER_GPU_REGISTERED_OUTPUT = 1,  // native input copy, direct final output
    OUSTER_GPU_REGISTERED_ALL = 2      // offline stable scan-pool experiment
};
struct ouster_gpu_config {
    uint32_t abi_version,struct_size,width,height,crop_start,crop_width;
    uint32_t min_range,max_range,ownership;
    // Row-major XYZ triples; copied during create. Shifts in [-width,width).
    const float* direction;
    const float* offset;
    const int32_t* shifts;
};
struct ouster_gpu_scan {
    const uint32_t* range;
    const uint16_t* signal;
    const uint8_t* reflectivity;
    const uint16_t* near_ir;
    const uint64_t* timestamps;
    uint64_t scan_ts;
    size_t pixels,columns;
};
typedef struct ouster_gpu_handle ouster_gpu_handle;
// Returns 0 on success. Error text is bounded and NUL terminated when provided.
// create/prepare failures have not launched any output writes. run returns 1
// for invalid arguments before submission, 2 for execution errors: the caller
// must stop this experimental GPU path on 2 (no silent fallback).
int ouster_gpu_create(const struct ouster_gpu_config*,ouster_gpu_handle**,char*,size_t);
// prepare registers the existing CPU-owned byte allocation; never resizes it.
// Direct registered output must be 16-byte aligned (checked, never assumed
// from the vector<uint8_t> element type). Staged output may be unaligned.
// For REGISTERED_ALL, scan_pool contains stable native array allocations, whose
// lifetime must extend until release. Other modes require an empty pool.
int ouster_gpu_prepare(ouster_gpu_handle*,uint8_t* output,size_t output_bytes,
                       const struct ouster_gpu_scan* scan_pool,size_t count,char*,size_t);
// Blocking: on success the complete CPU-readable point bytes and density are
// ready. Does not publish, allocate a message, retain input pointers (output-only
// mode), or register/unregister on the successful per-frame output-only path.
int ouster_gpu_run(ouster_gpu_handle*,const struct ouster_gpu_scan*,uint8_t*,size_t,int*,char*,size_t);
// Call BEFORE output storage can reallocate/free or its owning message can be
// replaced, even when CPU subscribers retain that old message. GPU work is
// complete before release returns. It does not invalidate CPU ownership/data.
int ouster_gpu_release(ouster_gpu_handle*,char*,size_t);
void ouster_gpu_destroy(ouster_gpu_handle*);
#ifdef __cplusplus
}
#endif
#endif
