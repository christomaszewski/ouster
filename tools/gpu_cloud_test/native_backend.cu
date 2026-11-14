#define CLOUD_NEXT_LIBRARY
#include "cloud_benchmark_next.cu"
#include "native_backend.h"

struct NativeInputs {
    const uint32_t* range;const uint16_t* signal;const uint8_t* reflectivity;
    const uint16_t* near_ir;const uint64_t* timestamps;
};
struct NativeLayout {
    size_t signal,reflectivity,near_ir,timestamps,bytes;
    NativeLayout(size_t n,size_t w):signal(n*4),reflectivity(n*6),near_ir((n*7+1)&~size_t(1)),timestamps((near_ir+n*2+7)&~size_t(7)),bytes(timestamps+w*8){}
    NativeInputs pointers(uint8_t* p) const {return {reinterpret_cast<uint32_t*>(p),reinterpret_cast<uint16_t*>(p+signal),p+reflectivity,reinterpret_cast<uint16_t*>(p+near_ir),reinterpret_cast<uint64_t*>(p+timestamps)};}
};
static bool same_scan(const ouster_gpu_scan& a,const ouster_gpu_scan& b) {
    return a.range==b.range&&a.signal==b.signal&&a.reflectivity==b.reflectivity&&a.near_ir==b.near_ir&&a.timestamps==b.timestamps&&a.pixels==b.pixels&&a.columns==b.columns;
}
struct ouster_gpu_handle {
    ouster_gpu_config config;size_t n,output_bytes;uint32_t blocks;NativeLayout layout;
    uint8_t *host_input=nullptr,*device_input=nullptr,*host_output=nullptr,*device_output=nullptr,*registered_output=nullptr;
    uint32_t *host_invalid=nullptr,*device_invalid=nullptr;ScanState *host_state=nullptr,*device_state=nullptr;
    float *direction=nullptr,*offset=nullptr;int32_t* shifts=nullptr;
    cudaStream_t stream=nullptr;cudaEvent_t ready=nullptr;std::unique_ptr<Registration> registration;
    std::vector<ouster_gpu_scan> pool;std::vector<NativeInputs> aliases;
    explicit ouster_gpu_handle(const ouster_gpu_config& c):config(c),n(size_t(c.width)*c.height),output_bytes(size_t(c.crop_width)*c.height*48),blocks((size_t(c.crop_width)*c.height+255)/256),layout(n,c.width) {
        try {
            CU(cudaSetDeviceFlags(cudaDeviceMapHost|cudaDeviceScheduleBlockingSync));int id=0,supported=0;CU(cudaGetDevice(&id));cudaDeviceProp p{};CU(cudaGetDeviceProperties(&p,id));
            CU(cudaDeviceGetAttribute(&supported,cudaDevAttrHostRegisterSupported,id));if(!p.canMapHostMemory||(c.ownership!=OUSTER_GPU_STAGED&&!supported))throw std::runtime_error("Mapped/registered host memory unsupported");
            CU(cudaStreamCreateWithFlags(&stream,cudaStreamNonBlocking));CU(cudaEventCreateWithFlags(&ready,cudaEventBlockingSync|cudaEventDisableTiming));
            CU(cudaHostAlloc(&host_invalid,blocks*4,cudaHostAllocMapped));device_invalid=device_alias(host_invalid);
            CU(cudaHostAlloc(&host_state,sizeof(ScanState),cudaHostAllocMapped));device_state=device_alias(host_state);
            if(c.ownership!=OUSTER_GPU_REGISTERED_ALL) {CU(cudaHostAlloc(&host_input,layout.bytes,cudaHostAllocMapped));device_input=device_alias(host_input);}
            if(c.ownership==OUSTER_GPU_STAGED) {CU(cudaHostAlloc(&host_output,output_bytes,cudaHostAllocMapped));device_output=device_alias(host_output);}
            std::vector<float> d(n*3),o(n*3);for(size_t i=0;i<n;i++)for(size_t axis=0;axis<3;axis++) {d[axis*n+i]=c.direction[i*3+axis];o[axis*n+i]=c.offset[i*3+axis];}
            std::vector<int32_t> s(c.shifts,c.shifts+c.height);for(auto& x:s)x=(x+int32_t(c.width))%int32_t(c.width);
            CU(cudaMalloc(&direction,n*12));CU(cudaMalloc(&offset,n*12));CU(cudaMalloc(&shifts,c.height*4));
            CU(cudaMemcpy(direction,d.data(),n*12,cudaMemcpyHostToDevice));CU(cudaMemcpy(offset,o.data(),n*12,cudaMemcpyHostToDevice));CU(cudaMemcpy(shifts,s.data(),c.height*4,cudaMemcpyHostToDevice));
            config.direction=config.offset=nullptr;config.shifts=nullptr;
        } catch(...) {cleanup();throw;}
    }
    void validate(const ouster_gpu_scan& s) const {
        if(s.pixels!=n||s.columns!=config.width||!s.range||!s.signal||!s.reflectivity||!s.near_ir||!s.timestamps)throw std::runtime_error("Native scan pointers/dimensions invalid");
        if(reinterpret_cast<uintptr_t>(s.range)%alignof(uint32_t)||reinterpret_cast<uintptr_t>(s.signal)%alignof(uint16_t)||reinterpret_cast<uintptr_t>(s.near_ir)%alignof(uint16_t)||reinterpret_cast<uintptr_t>(s.timestamps)%alignof(uint64_t))throw std::runtime_error("Native scan field alignment invalid");
    }
    void release_checked() {if(stream)CU(cudaStreamSynchronize(stream));if(registration)registration->close();registration.reset();pool.clear();aliases.clear();registered_output=nullptr;if(config.ownership!=OUSTER_GPU_STAGED)device_output=nullptr;}
    void cleanup() noexcept {if(stream)cudaStreamSynchronize(stream);registration.reset();cudaFree(direction);cudaFree(offset);cudaFree(shifts);cudaFreeHost(host_input);cudaFreeHost(host_output);cudaFreeHost(host_invalid);cudaFreeHost(host_state);if(ready)cudaEventDestroy(ready);if(stream)cudaStreamDestroy(stream);}
    ~ouster_gpu_handle(){cleanup();}
    void prepare(uint8_t* out,size_t bytes,const ouster_gpu_scan* scans,size_t count) {
        if(!out||bytes!=output_bytes)throw std::runtime_error("Native output byte span invalid");
        if(config.ownership!=OUSTER_GPU_STAGED&&reinterpret_cast<uintptr_t>(out)%16)throw std::runtime_error("Registered output must be 16-byte aligned");
        if(config.ownership!=OUSTER_GPU_REGISTERED_ALL&&count)throw std::runtime_error("Only stable-input mode accepts a scan pool");
        if(config.ownership==OUSTER_GPU_REGISTERED_ALL&&(!scans||!count))throw std::runtime_error("Stable native scan pool missing");
        release_checked();if(config.ownership==OUSTER_GPU_STAGED)return;
        std::vector<HostSpan> spans{{out,bytes}};for(size_t i=0;i<count;i++) {
            validate(scans[i]);const auto& s=scans[i];spans.insert(spans.end(),{{const_cast<uint32_t*>(s.range),n*4},{const_cast<uint16_t*>(s.signal),n*2},{const_cast<uint8_t*>(s.reflectivity),n},{const_cast<uint16_t*>(s.near_ir),n*2},{const_cast<uint64_t*>(s.timestamps),size_t(config.width)*8}});
        }
        auto candidate=std::make_unique<Registration>(spans);std::vector<NativeInputs> mapped;std::vector<ouster_gpu_scan> new_pool;
        mapped.reserve(count);new_pool.reserve(count);
        for(size_t i=0;i<count;i++) {const auto& s=scans[i];mapped.push_back({device_alias(const_cast<uint32_t*>(s.range)),device_alias(const_cast<uint16_t*>(s.signal)),device_alias(const_cast<uint8_t*>(s.reflectivity)),device_alias(const_cast<uint16_t*>(s.near_ir)),device_alias(const_cast<uint64_t*>(s.timestamps))});new_pool.push_back(s);}
        auto* alias=device_alias(out);if(reinterpret_cast<uintptr_t>(alias)%16)throw std::runtime_error("Mapped output alignment invalid");registration=std::move(candidate);pool=std::move(new_pool);aliases=std::move(mapped);registered_output=out;device_output=alias;
    }
    void validate_run(const ouster_gpu_scan& s,uint8_t* out,size_t bytes) const {
        validate(s);if(!out||bytes!=output_bytes)throw std::runtime_error("Native output byte span invalid");
        if(config.ownership!=OUSTER_GPU_STAGED&&(!registration||out!=registered_output))throw std::runtime_error("Native output is not the prepared allocation");
        if(config.ownership==OUSTER_GPU_REGISTERED_ALL&&std::none_of(pool.begin(),pool.end(),[&](const auto& p){return same_scan(s,p);}))throw std::runtime_error("Native scan is outside registered pool");
    }
    bool run(const ouster_gpu_scan& s,uint8_t* out,size_t bytes) {
        NativeInputs in{};
        if(config.ownership==OUSTER_GPU_REGISTERED_ALL) {
            size_t i=0;while(i<pool.size()&&!same_scan(s,pool[i]))i++;if(i==pool.size())throw std::runtime_error("Native scan is outside registered pool");in=aliases[i];
        } else {
            std::memcpy(host_input,s.range,n*4);std::memcpy(host_input+layout.signal,s.signal,n*2);std::memcpy(host_input+layout.reflectivity,s.reflectivity,n);std::memcpy(host_input+layout.near_ir,s.near_ir,n*2);std::memcpy(host_input+layout.timestamps,s.timestamps,size_t(config.width)*8);in=layout.pointers(device_input);
        }
        *host_state={s.scan_ts,config.min_range,config.max_range};
        make_cloud_soa<256,false><<<blocks,256,0,stream>>>(in,direction,offset,shifts,config.width,config.height,config.crop_start,config.crop_width,device_state,device_output,device_invalid);CU(cudaGetLastError());
        CU(cudaEventRecord(ready,stream));CU(cudaEventSynchronize(ready));
        if(config.ownership==OUSTER_GPU_STAGED)std::memcpy(out,host_output,bytes);
        return std::all_of(host_invalid,host_invalid+blocks,[](uint32_t x){return x==0;});
    }
};
static void error_text(char* dst,size_t n,const char* text) noexcept {if(dst&&n){std::strncpy(dst,text,n-1);dst[n-1]=0;}}
extern "C" int ouster_gpu_create(const ouster_gpu_config* c,ouster_gpu_handle** out,char* error,size_t len) {
    if(out)*out=nullptr;
    try {
        if(!c||!out||c->abi_version!=OUSTER_GPU_ABI_VERSION||c->struct_size!=sizeof(*c)||!c->width||!c->height||c->height>65536||size_t(c->width)*c->height>16777216||!c->crop_width||c->crop_width>c->width||c->crop_start>=c->width||c->min_range>=c->max_range||c->ownership>OUSTER_GPU_REGISTERED_ALL||!c->direction||!c->offset||!c->shifts)throw std::runtime_error("Invalid native GPU configuration/ABI");
        for(uint32_t row=0;row<c->height;row++)if(c->shifts[row]<-int64_t(c->width)||c->shifts[row]>=int64_t(c->width))throw std::runtime_error("Invalid native GPU shift");
        *out=new ouster_gpu_handle(*c);return 0;
    } catch(const std::exception& e){error_text(error,len,e.what());return 1;}
}
extern "C" int ouster_gpu_prepare(ouster_gpu_handle* h,uint8_t* out,size_t bytes,const ouster_gpu_scan* scans,size_t count,char* error,size_t len) {try {if(!h)throw std::runtime_error("Null GPU handle");h->prepare(out,bytes,scans,count);return 0;}catch(const std::exception& e){error_text(error,len,e.what());return 1;}}
extern "C" int ouster_gpu_run(ouster_gpu_handle* h,const ouster_gpu_scan* scan,uint8_t* out,size_t bytes,int* dense,char* error,size_t len) {
    try {if(!h||!scan||!dense)throw std::runtime_error("Null GPU run argument");h->validate_run(*scan,out,bytes);}catch(const std::exception& e){error_text(error,len,e.what());return 1;}
    try {*dense=h->run(*scan,out,bytes)?1:0;return 0;}catch(const std::exception& e){cudaStreamSynchronize(h->stream);error_text(error,len,e.what());return 2;}
}
extern "C" int ouster_gpu_release(ouster_gpu_handle* h,char* error,size_t len) {try {if(h)h->release_checked();return 0;}catch(const std::exception& e){error_text(error,len,e.what());return 1;}}
extern "C" void ouster_gpu_destroy(ouster_gpu_handle* h){delete h;}
