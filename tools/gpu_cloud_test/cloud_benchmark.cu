#include "cloud_common.hpp"
#include <chrono>
#include <ctime>
#include <functional>
#include <iomanip>
#include <iostream>
#include <memory>
#include <numeric>
#ifndef CLOUD_CPU_ONLY
#include <cuda_runtime.h>
#endif
using namespace cloud_test;
using Clock=std::chrono::steady_clock;
static volatile uint64_t keep_result=0;

#ifndef CLOUD_CPU_ONLY
inline void check_cuda(cudaError_t e,const char* operation) {
    if(e!=cudaSuccess)throw std::runtime_error(std::string(operation)+": "+cudaGetErrorString(e));
}
#define CU(x) check_cuda((x),#x)
struct Layout {
    size_t range,signal,reflectivity,near_ir,timestamps,bytes;
    explicit Layout(size_t n,uint32_t w) :range(0),signal(n*4),reflectivity(n*8),near_ir(n*10),timestamps((n*12+7)&~size_t(7)),bytes(timestamps+size_t(w)*8){}
};
struct Inputs {const uint32_t *range,*signal;const uint16_t *reflectivity,*near_ir;const uint64_t* timestamps;};
inline Inputs pointers(void* p,const Layout& l) {
    auto* b=static_cast<uint8_t*>(p);return {reinterpret_cast<uint32_t*>(b+l.range),reinterpret_cast<uint32_t*>(b+l.signal),reinterpret_cast<uint16_t*>(b+l.reflectivity),reinterpret_cast<uint16_t*>(b+l.near_ir),reinterpret_cast<uint64_t*>(b+l.timestamps)};
}
__global__ void make_cloud(Inputs in,const float* direction,const float* offset,const int32_t* shifts,
                          uint32_t width,uint32_t height,uint32_t start,uint32_t out_width,
                          uint32_t min_range,uint32_t max_range,uint64_t scan_ts,
                          Point48* out,uint32_t* block_invalid) {
    __shared__ uint32_t warp_invalid[8];
    size_t index=size_t(blockIdx.x)*blockDim.x+threadIdx.x;
    bool invalid=false;
    if(index<size_t(height)*out_width){
        uint32_t row=index/out_width,col=index%out_width,global=start+col;
        if(global>=width)global-=width;
        int32_t src_col=int32_t(global)-shifts[row];if(src_col<0)src_col+=width;
        size_t src=size_t(row)*width+uint32_t(src_col);uint32_t range=in.range[src];
        Point48 p{};p.homogeneous=1.0f;
        if(range<=min_range||range>=max_range){p.x=p.y=p.z=__uint_as_float(0x7fc00000);}
        else {float r=__uint2float_rn(range);p.x=__fmaf_rn(r,direction[src*3],offset[src*3]);p.y=__fmaf_rn(r,direction[src*3+1],offset[src*3+1]);p.z=__fmaf_rn(r,direction[src*3+2],offset[src*3+2]);}
        p.intensity=__uint2float_rn(in.signal[src]);uint64_t ts=in.timestamps[src_col];p.t=uint32_t(ts>scan_ts?ts-scan_ts:0);
        p.reflectivity=in.reflectivity[src];p.ring=uint16_t(row);p.ambient=in.near_ir[src];p.range=range;
        invalid=isnan(p.x)||isnan(p.y)||isnan(p.z);out[index]=p;
    }
    uint32_t ballot=__ballot_sync(0xffffffff,invalid);
    if((threadIdx.x&31)==0)warp_invalid[threadIdx.x/32]=__popc(ballot);
    __syncthreads();
    if(threadIdx.x==0){uint32_t sum=0;for(unsigned i=0;i<8;i++)sum+=warp_invalid[i];block_invalid[blockIdx.x]=sum;}
}
class GpuPlan {
    Layout layout_;Roi roi_;uint32_t width_,height_,blocks_;bool mapped_;
    uint8_t *host_input_=nullptr,*device_input_=nullptr;
    Point48 *host_output_=nullptr,*device_output_=nullptr;
    uint32_t *host_invalid_=nullptr,*device_invalid_=nullptr;
    float *direction_=nullptr,*offset_=nullptr;int32_t* shifts_=nullptr;
    cudaStream_t stream_=nullptr;cudaEvent_t ready_=nullptr;
    void release() noexcept {
        if(stream_)cudaStreamSynchronize(stream_);
        if(!mapped_){cudaFree(device_input_);cudaFree(device_output_);cudaFree(device_invalid_);}
        cudaFree(direction_);cudaFree(offset_);cudaFree(shifts_);
        cudaFreeHost(host_input_);cudaFreeHost(host_output_);cudaFreeHost(host_invalid_);
        if(ready_)cudaEventDestroy(ready_);if(stream_)cudaStreamDestroy(stream_);
    }
public:
    GpuPlan(const Frame& f,Roi roi,bool mapped):layout_(f.range.size(),f.width),roi_(roi),width_(f.width),height_(f.height),blocks_((size_t(f.height)*roi.width+255)/256),mapped_(mapped) {
        try {
            CU(cudaStreamCreateWithFlags(&stream_,cudaStreamNonBlocking));
            CU(cudaEventCreateWithFlags(&ready_,cudaEventBlockingSync|cudaEventDisableTiming));
            unsigned flags=mapped?cudaHostAllocMapped:cudaHostAllocDefault;
            CU(cudaHostAlloc(&host_input_,layout_.bytes,flags));CU(cudaHostAlloc(&host_output_,size_t(f.height)*roi.width*48,flags));CU(cudaHostAlloc(&host_invalid_,blocks_*sizeof(uint32_t),flags));
            if(mapped){CU(cudaHostGetDevicePointer(&device_input_,host_input_,0));CU(cudaHostGetDevicePointer(&device_output_,host_output_,0));CU(cudaHostGetDevicePointer(&device_invalid_,host_invalid_,0));}
            else {CU(cudaMalloc(&device_input_,layout_.bytes));CU(cudaMalloc(&device_output_,size_t(f.height)*roi.width*48));CU(cudaMalloc(&device_invalid_,blocks_*sizeof(uint32_t)));}
            CU(cudaMalloc(&direction_,f.direction.size()*4));CU(cudaMalloc(&offset_,f.offset.size()*4));CU(cudaMalloc(&shifts_,f.shifts.size()*4));
            CU(cudaMemcpy(direction_,f.direction.data(),f.direction.size()*4,cudaMemcpyHostToDevice));CU(cudaMemcpy(offset_,f.offset.data(),f.offset.size()*4,cudaMemcpyHostToDevice));
            std::vector<int32_t> normalized=f.shifts;for(auto& s:normalized)s=(s+int32_t(f.width))%int32_t(f.width);
            CU(cudaMemcpy(shifts_,normalized.data(),normalized.size()*4,cudaMemcpyHostToDevice));
        } catch(...) {release();throw;}
    }
    ~GpuPlan(){release();}
    GpuPlan(const GpuPlan&)=delete;GpuPlan& operator=(const GpuPlan&)=delete;
    bool run(const Frame& f,std::vector<Point48>& output) {
        // Both modes include copying the decoded pageable scan into persistent
        // pinned input. Neither assumes a production GPU-aware scan allocator.
        size_t n=f.range.size();std::memcpy(host_input_+layout_.range,f.range.data(),n*4);std::memcpy(host_input_+layout_.signal,f.signal.data(),n*4);
        std::memcpy(host_input_+layout_.reflectivity,f.reflectivity.data(),n*2);std::memcpy(host_input_+layout_.near_ir,f.near_ir.data(),n*2);std::memcpy(host_input_+layout_.timestamps,f.timestamps.data(),size_t(f.width)*8);
        if(!mapped_)CU(cudaMemcpyAsync(device_input_,host_input_,layout_.bytes,cudaMemcpyHostToDevice,stream_));
        make_cloud<<<blocks_,256,0,stream_>>>(pointers(device_input_,layout_),direction_,offset_,shifts_,width_,height_,roi_.start,roi_.width,f.min_range,f.max_range,f.scan_ts,device_output_,device_invalid_);
        CU(cudaGetLastError());
        if(!mapped_){CU(cudaMemcpyAsync(host_output_,device_output_,output.size()*48,cudaMemcpyDeviceToHost,stream_));CU(cudaMemcpyAsync(host_invalid_,device_invalid_,blocks_*4,cudaMemcpyDeviceToHost,stream_));}
        CU(cudaEventRecord(ready_,stream_));CU(cudaEventSynchronize(ready_));
        // Include materialization into an ordinary host-owned cloud buffer,
        // compatible with the current ROS CPU buffer. Publication is not timed.
        std::memcpy(output.data(),host_output_,output.size()*48);
        return std::all_of(host_invalid_,host_invalid_+blocks_,[](uint32_t n){return n==0;});
    }
};
#endif

struct Options {std::string input,output;bool synthetic=false,cpu_only=false,validate_only=false;unsigned iterations=64,rounds=7,warmup=16;Roi roi;};
Options options(int argc,char** argv) {
    Options o;
    for(int i=1;i<argc;i++){std::string a=argv[i];auto value=[&](){if(++i>=argc)throw std::runtime_error("Missing argument for "+a);return std::string(argv[i]);};
        if(a=="--input")o.input=value();else if(a=="--synthetic")o.synthetic=true;else if(a=="--cpu-only")o.cpu_only=true;else if(a=="--validate-only")o.validate_only=true;
        else if(a=="--iterations")o.iterations=std::stoul(value());else if(a=="--rounds")o.rounds=std::stoul(value());else if(a=="--warmup")o.warmup=std::stoul(value());
        else if(a=="--crop-start")o.roi.start=std::stoul(value());else if(a=="--crop-width")o.roi.width=std::stoul(value());else if(a=="--output-dir")o.output=value();else throw std::runtime_error("Unknown argument "+a);
    }
    if(o.synthetic==!o.input.empty())throw std::runtime_error("Specify exactly one of --input directory or --synthetic");
    if(!o.iterations||!o.rounds||o.rounds>1000||o.iterations>100000)throw std::runtime_error("Invalid benchmark bounds");return o;
}
struct Method {std::string name;std::function<bool(const Frame&,std::vector<Point48>&)> run;};
void check_config(const std::vector<Frame>& frames) {
    const auto& first=frames.front();for(const auto& f:frames){validate_frame(f);if(f.width!=first.width||f.height!=first.height||f.direction!=first.direction||f.offset!=first.offset||f.shifts!=first.shifts)throw std::runtime_error("Fixture LUT/dimensions differ; run separate benchmark groups");}
}
void validation(const std::vector<Frame>& frames,Roi roi,const std::vector<Method>& methods) {
    size_t n=size_t(frames[0].height)*roi.width;
    for(const auto& f:frames){std::vector<Point48> expected(n),actual(n);bool dense=cpu_fused(f,roi,expected);
        if(!f.reference.empty())compare(crop_reference(f,roi),expected,f.name+" production reference");
        for(auto& method:methods){bool got=method.run(f,actual);compare(expected,actual,f.name+" "+method.name);if(got!=dense)throw std::runtime_error("is_dense differs: "+method.name);}
    }
}
#ifndef CLOUD_BENCHMARK_LIBRARY
int main(int argc,char** argv) try {
    Options o=options(argc,argv);
#ifdef CLOUD_CPU_ONLY
    o.cpu_only=true;
#endif
    std::vector<Frame> frames=o.synthetic?std::vector<Frame>{synthetic(2048,128,1),synthetic(2048,128,2),synthetic(2048,128,3),synthetic(2048,128,4)}:read_frames(o.input);
    // Synthetic frames use one persistent LUT too, matching fixed calibration.
    if(o.synthetic)for(size_t i=1;i<frames.size();i++){frames[i].direction=frames[0].direction;frames[i].offset=frames[0].offset;}
    check_config(frames);auto& first=frames.front();if(!o.roi.width)o.roi.width=first.width;
    if(o.roi.start>=first.width||!o.roi.width||o.roi.width>first.width)throw std::runtime_error("ROI outside global destaggered column grid");
    std::vector<std::array<float,3>> scratch(first.range.size());
    std::vector<Method> methods{{"cpu_staged",[&](const Frame& f,std::vector<Point48>& out){return cpu_staged(f,o.roi,scratch,out);}},
                                {"cpu_fused",[&](const Frame& f,std::vector<Point48>& out){return cpu_fused(f,o.roi,out);}}};
#ifndef CLOUD_CPU_ONLY
    std::unique_ptr<GpuPlan> copy,mapped;
    if(!o.cpu_only){CU(cudaSetDeviceFlags(cudaDeviceMapHost|cudaDeviceScheduleBlockingSync));int id=0;CU(cudaGetDevice(&id));cudaDeviceProp prop{};CU(cudaGetDeviceProperties(&prop,id));
        if(!prop.canMapHostMemory)throw std::runtime_error("GPU cannot map host memory");
        std::cerr<<"GPU="<<prop.name<<" cc="<<prop.major<<'.'<<prop.minor<<" integrated="<<prop.integrated<<" managed="<<prop.managedMemory<<" concurrentManagedAccess="<<prop.concurrentManagedAccess<<'\n';
        copy=std::make_unique<GpuPlan>(first,o.roi,false);mapped=std::make_unique<GpuPlan>(first,o.roi,true);
        methods.push_back({"gpu_device_copy",[&](const Frame& f,std::vector<Point48>& out){return copy->run(f,out);}});
        methods.push_back({"gpu_mapped_io",[&](const Frame& f,std::vector<Point48>& out){return mapped->run(f,out);}});
    }
#endif
    validation(frames,o.roi,methods);
    // Small odd grids exercise partial CUDA blocks, tail warps, wrapping ROI,
    // both shift signs, invalid boundaries and overflowing relative timestamps.
    if(o.synthetic){for(auto shape:std::vector<std::pair<uint32_t,uint32_t>>{{17,5},{32,3},{1,1}}){Frame f=synthetic(shape.first,shape.second,7);Roi roi{shape.first>1?shape.first-2:0,shape.first};
        std::vector<std::array<float,3>> small_scratch(f.range.size());std::vector<Method> small{{"cpu_staged",[&](const Frame& s,std::vector<Point48>& out){return cpu_staged(s,roi,small_scratch,out);}}, {"cpu_fused",[&](const Frame& s,std::vector<Point48>& out){return cpu_fused(s,roi,out);}}};
#ifndef CLOUD_CPU_ONLY
        std::unique_ptr<GpuPlan> g1,g2;if(!o.cpu_only){g1=std::make_unique<GpuPlan>(f,roi,false);g2=std::make_unique<GpuPlan>(f,roi,true);small.push_back({"gpu_device_copy",[&](const Frame& s,std::vector<Point48>& out){return g1->run(s,out);}});small.push_back({"gpu_mapped_io",[&](const Frame& s,std::vector<Point48>& out){return g2->run(s,out);}});}
#endif
        auto valid=f;valid.name="all_valid";std::fill(valid.range.begin(),valid.range.end(),1000);
        auto invalid=f;invalid.name="all_invalid";std::fill(invalid.range.begin(),invalid.range.end(),0);
        auto large=f;large.name="u32_conversion";large.max_range=UINT32_MAX;std::fill(large.range.begin(),large.range.end(),UINT32_MAX-1);std::fill(large.signal.begin(),large.signal.end(),UINT32_MAX);large.range[0]=UINT32_MAX;
        validation({f,valid,invalid,large},roi,small);
    }}
    std::cerr<<"Validation PASS: frames="<<frames.size()<<" methods="<<methods.size()<<" width="<<o.roi.width<<" height="<<first.height<<" point_step=48 real="<<(first.real?1:0)<<"\n";
    if(o.validate_only)return 0;
    std::vector<Point48> output(size_t(first.height)*o.roi.width);
    for(auto& m:methods)for(unsigned i=0;i<o.warmup;i++)m.run(frames[i%frames.size()],output);
    std::cout<<"source,crop_start,crop_width,height,round,order,implementation,iterations,wall_ms,cpu_ms,checksum,is_dense\n"<<std::setprecision(12);
    for(unsigned round=0;round<o.rounds;round++)for(size_t order=0;order<methods.size();order++){
        auto& m=methods[(order+round)%methods.size()];bool dense=false;auto begin=Clock::now();auto cpu=std::clock();
        for(unsigned i=0;i<o.iterations;i++){dense=m.run(frames[i%frames.size()],output);keep_result=output[0].range;}
        double cpu_ms=1000.0*(std::clock()-cpu)/CLOCKS_PER_SEC;double wall_ms=std::chrono::duration<double,std::milli>(Clock::now()-begin).count();
        // Verification and hashing are outside the timed window.
        auto& last=frames[(o.iterations-1)%frames.size()];std::vector<Point48> expected(output.size());bool expected_dense=cpu_fused(last,o.roi,expected);compare(expected,output,m.name+" timed result");if(dense!=expected_dense)throw std::runtime_error("Timed density mismatch");
        std::cout<<(first.real?"real":"synthetic")<<','<<o.roi.start<<','<<o.roi.width<<','<<first.height<<','<<round<<','<<order<<','<<m.name<<','<<o.iterations<<','<<wall_ms<<','<<cpu_ms<<','<<checksum(output)<<','<<(dense?1:0)<<'\n';
    }
    if(!o.output.empty()){fs::create_directories(o.output);for(auto& m:methods){m.run(frames.front(),output);write_binary(fs::path(o.output)/(m.name+".cloud48"),output.data(),output.size());}}
    return 0;
}catch(const std::exception& e){std::cerr<<"ERROR: "<<e.what()<<'\n';return 1;}
#endif
