// Extended standalone experiments. The original executable/methods remain the
// control; this file does not change any production allocator or CUDA backend.
#define CLOUD_BENCHMARK_LIBRARY
#include "cloud_benchmark.cu"
#include <set>
#include <sstream>
#include <thread>
#include <unistd.h>

struct NextOptions {
    Options base;
    std::set<std::string> selected;
    double period_ms=0;
};
NextOptions next_options(int argc,char** argv) {
    NextOptions n;std::vector<char*> base{argv[0]};
    for(int i=1;i<argc;i++) {
        std::string a=argv[i];
        if(a=="--period-ms"||a=="--methods") {
            if(++i>=argc)throw std::runtime_error("Missing argument for "+a);
            if(a=="--period-ms")n.period_ms=std::stod(argv[i]);
            else {std::istringstream in(argv[i]);std::string name;while(std::getline(in,name,',')) {
                if(name.empty()||!n.selected.insert(name).second)throw std::runtime_error("Empty/duplicate method selection");
            }if(n.selected.empty())throw std::runtime_error("Empty method selection");}
        } else base.push_back(argv[i]);
    }
    n.base=options(int(base.size()),base.data());
    if(!std::isfinite(n.period_ms)||n.period_ms<0||n.period_ms>1000||n.base.warmup>100000)
        throw std::runtime_error("Invalid pacing/warmup bounds");
    return n;
}

#ifndef CLOUD_CPU_ONLY
struct ScanState {uint64_t scan_ts;uint32_t min_range,max_range;};

template<unsigned Threads,bool Tiled,class Input=Inputs>
__global__ void make_cloud_soa(Input in,const float* direction,const float* offset,const int32_t* shifts,
                             uint32_t width,uint32_t height,uint32_t start,uint32_t out_width,
                             const ScanState* state,unsigned char* out,uint32_t* block_invalid) {
    // Callers establish 16-byte device-output alignment. This is an alignment
    // hint only: memcpy still writes byte storage, not Point48 object lvalues.
    out=static_cast<unsigned char*>(__builtin_assume_aligned(out,16));
    static_assert(Threads%32==0 && Threads<=1024);
    __shared__ uint32_t warp_invalid[Threads/32];
    __shared__ Point48 tile[Tiled?Threads:1];
    size_t index=size_t(blockIdx.x)*Threads+threadIdx.x,n=size_t(height)*out_width,plane=size_t(width)*height;
    Point48 p{};bool invalid=false;
    if(index<n) {
        uint32_t row=index/out_width,col=index%out_width,global=start+col;
        if(global>=width)global-=width;
        int32_t src_col=int32_t(global)-shifts[row];if(src_col<0)src_col+=width;
        size_t src=size_t(row)*width+uint32_t(src_col);uint32_t range=in.range[src];
        p.homogeneous=1.0f;
        if(range<=state->min_range||range>=state->max_range)p.x=p.y=p.z=__uint_as_float(0x7fc00000);
        else {float r=__uint2float_rn(range);p.x=__fmaf_rn(r,direction[src],offset[src]);
            p.y=__fmaf_rn(r,direction[plane+src],offset[plane+src]);
            p.z=__fmaf_rn(r,direction[2*plane+src],offset[2*plane+src]);}
        p.intensity=__uint2float_rn(in.signal[src]);uint64_t ts=in.timestamps[src_col];
        p.t=uint32_t(ts>state->scan_ts?ts-state->scan_ts:0);
        p.reflectivity=in.reflectivity[src];p.ring=uint16_t(row);p.ambient=in.near_ir[src];p.range=range;
        invalid=isnan(p.x)||isnan(p.y)||isnan(p.z);
        if constexpr(!Tiled)memcpy(out+index*sizeof(Point48),&p,sizeof(Point48));
    }
    if constexpr(Tiled) {
        // A block transposes the 48-byte records into contiguous 16-byte stores.
        // Byte copies avoid type-punning the host Point48 object representation.
        tile[threadIdx.x]=p;__syncthreads();
        for(unsigned part=0;part<3;part++) {
            size_t v=threadIdx.x+size_t(part)*Threads,global_v=size_t(blockIdx.x)*Threads*3+v;
            if(global_v<n*3) {
                alignas(16) uint4 words;
                memcpy(&words,reinterpret_cast<const unsigned char*>(tile)+v*16,16);
                memcpy(reinterpret_cast<unsigned char*>(out)+global_v*16,&words,16);
            }
        }
    }
    uint32_t ballot=__ballot_sync(0xffffffff,invalid);
    if((threadIdx.x&31)==0)warp_invalid[threadIdx.x/32]=__popc(ballot);
    __syncthreads();
    if(threadIdx.x==0) {uint32_t sum=0;for(unsigned i=0;i<Threads/32;i++)sum+=warp_invalid[i];block_invalid[blockIdx.x]=sum;}
}

struct HostSpan {void* data;size_t bytes;};
template<class T> HostSpan span(const std::vector<T>& v) {return {const_cast<T*>(v.data()),v.size()*sizeof(T)};}
std::vector<HostSpan> frame_spans(const Frame& f) {return {span(f.range),span(f.signal),span(f.reflectivity),span(f.near_ir),span(f.timestamps)};}

// CUDA registration covers pages. Merge all overlapping/touching page ranges
// before registering, including small fixture arrays that share malloc pages.
// The underlying allocations must outlive this object and must never move.
class Registration {
    std::vector<void*> registered_;
    size_t bytes_=0;
public:
    explicit Registration(const std::vector<HostSpan>& spans) {
        long page=sysconf(_SC_PAGESIZE);if(page<=0)throw std::runtime_error("Cannot determine host page size");
        std::vector<std::pair<uintptr_t,uintptr_t>> ranges;
        for(auto s:spans) {
            uintptr_t p=reinterpret_cast<uintptr_t>(s.data);
            if(!s.data||!s.bytes||s.bytes>UINTPTR_MAX-p||p+s.bytes>UINTPTR_MAX-uintptr_t(page-1))
                throw std::runtime_error("Invalid host registration span");
            ranges.push_back({p/uintptr_t(page)*uintptr_t(page),(p+s.bytes+page-1)/page*page});
        }
        std::sort(ranges.begin(),ranges.end());std::vector<std::pair<uintptr_t,uintptr_t>> merged;
        for(auto r:ranges) {if(!merged.empty()&&r.first<=merged.back().second)merged.back().second=std::max(merged.back().second,r.second);else merged.push_back(r);}
        registered_.reserve(merged.size());
        try {for(auto r:merged) {void* p=reinterpret_cast<void*>(r.first);CU(cudaHostRegister(p,r.second-r.first,cudaHostRegisterMapped));registered_.push_back(p);bytes_+=r.second-r.first;}}
        catch(...) {clear();throw;}
    }
    void clear() noexcept {for(auto p:registered_)cudaHostUnregister(p);registered_.clear();}
    void close() {while(!registered_.empty()) {CU(cudaHostUnregister(registered_.back()));registered_.pop_back();}}
    ~Registration(){clear();}
    Registration(const Registration&)=delete;Registration& operator=(const Registration&)=delete;
    size_t bytes() const{return bytes_;}
};
template<class T> T* device_alias(T* host) {T* device=nullptr;CU(cudaHostGetDevicePointer(&device,host,0));return device;}
Inputs registered_inputs(const Frame& f) {
    return {device_alias(const_cast<uint32_t*>(f.range.data())),device_alias(const_cast<uint32_t*>(f.signal.data())),
            device_alias(const_cast<uint16_t*>(f.reflectivity.data())),device_alias(const_cast<uint16_t*>(f.near_ir.data())),
            device_alias(const_cast<uint64_t*>(f.timestamps.data()))};
}
enum class Ownership {Staged,RegisterOutput,RegisterAll,RegisterEach};
const char* contract(Ownership o) {
    switch(o) {case Ownership::Staged:return "ordinary_host_copies";
        case Ownership::RegisterOutput:return "persistent_registered_output";
        case Ownership::RegisterAll:return "persistent_registered_scan_and_output";
        case Ownership::RegisterEach:return "ordinary_host_per_frame_registration";}
    return "invalid";
}
class NextPlan {
    Layout layout_;Roi roi_;uint32_t width_,height_,blocks_,threads_;bool tiled_,graph_;Ownership ownership_;
    uint8_t* host_input_=nullptr;uint8_t* input_alias_=nullptr;
    Point48* host_output_=nullptr;Point48* output_alias_=nullptr;
    uint32_t* host_invalid_=nullptr;uint32_t* invalid_alias_=nullptr;
    ScanState* host_state_=nullptr;ScanState* state_alias_=nullptr;
    float *direction_=nullptr,*offset_=nullptr;int32_t* shifts_=nullptr;
    cudaStream_t stream_=nullptr;cudaEvent_t ready_=nullptr;cudaGraph_t graph_handle_=nullptr;cudaGraphExec_t graph_exec_=nullptr;
    std::unique_ptr<Registration> registration_;
    std::vector<const Frame*> registered_frames_;std::vector<Inputs> registered_inputs_,registered_host_inputs_;
    Point48* expected_output_=nullptr;size_t expected_size_=0;
    void launch(Inputs in,Point48* out) {
        auto* bytes=reinterpret_cast<unsigned char*>(out);
        if(tiled_)make_cloud_soa<256,true><<<blocks_,256,0,stream_>>>(in,direction_,offset_,shifts_,width_,height_,roi_.start,roi_.width,state_alias_,bytes,invalid_alias_);
        else if(threads_==128)make_cloud_soa<128,false><<<blocks_,128,0,stream_>>>(in,direction_,offset_,shifts_,width_,height_,roi_.start,roi_.width,state_alias_,bytes,invalid_alias_);
        else if(threads_==256)make_cloud_soa<256,false><<<blocks_,256,0,stream_>>>(in,direction_,offset_,shifts_,width_,height_,roi_.start,roi_.width,state_alias_,bytes,invalid_alias_);
        else make_cloud_soa<512,false><<<blocks_,512,0,stream_>>>(in,direction_,offset_,shifts_,width_,height_,roi_.start,roi_.width,state_alias_,bytes,invalid_alias_);
        CU(cudaGetLastError());
    }
    void release() noexcept {
        if(stream_)cudaStreamSynchronize(stream_);
        if(graph_exec_)cudaGraphExecDestroy(graph_exec_);if(graph_handle_)cudaGraphDestroy(graph_handle_);
        registration_.reset();cudaFree(direction_);cudaFree(offset_);cudaFree(shifts_);
        cudaFreeHost(host_input_);cudaFreeHost(host_output_);cudaFreeHost(host_invalid_);cudaFreeHost(host_state_);
        if(ready_)cudaEventDestroy(ready_);if(stream_)cudaStreamDestroy(stream_);
    }
public:
    NextPlan(const std::vector<Frame>& frames,Roi roi,std::vector<Point48>& output,unsigned threads,bool tiled,bool graph,Ownership ownership):
        layout_(frames.front().range.size(),frames.front().width),roi_(roi),width_(frames.front().width),height_(frames.front().height),
        blocks_((size_t(height_)*roi.width+threads-1)/threads),threads_(threads),tiled_(tiled),graph_(graph),ownership_(ownership),
        expected_output_(output.data()),expected_size_(output.size()) {
        if((threads!=128&&threads!=256&&threads!=512)||(tiled&&threads!=256)||(graph&&ownership!=Ownership::Staged))
            throw std::runtime_error("Unsupported kernel/ownership configuration");
        try {
            CU(cudaStreamCreateWithFlags(&stream_,cudaStreamNonBlocking));CU(cudaEventCreateWithFlags(&ready_,cudaEventBlockingSync|cudaEventDisableTiming));
            CU(cudaHostAlloc(&host_invalid_,blocks_*4,cudaHostAllocMapped));invalid_alias_=device_alias(host_invalid_);
            CU(cudaHostAlloc(&host_state_,sizeof(ScanState),cudaHostAllocMapped));state_alias_=device_alias(host_state_);
            if(ownership==Ownership::Staged||ownership==Ownership::RegisterOutput) {CU(cudaHostAlloc(&host_input_,layout_.bytes,cudaHostAllocMapped));input_alias_=device_alias(host_input_);}
            if(ownership==Ownership::Staged) {CU(cudaHostAlloc(&host_output_,expected_size_*48,cudaHostAllocMapped));output_alias_=device_alias(host_output_);}
            if(ownership==Ownership::RegisterOutput||ownership==Ownership::RegisterAll) {
                std::vector<HostSpan> spans{span(output)};
                if(ownership==Ownership::RegisterAll)for(const auto& f:frames) {auto s=frame_spans(f);spans.insert(spans.end(),s.begin(),s.end());}
                registration_=std::make_unique<Registration>(spans);output_alias_=device_alias(output.data());
                if(ownership==Ownership::RegisterAll)for(const auto& f:frames) {registered_frames_.push_back(&f);registered_inputs_.push_back(registered_inputs(f));registered_host_inputs_.push_back({f.range.data(),f.signal.data(),f.reflectivity.data(),f.near_ir.data(),f.timestamps.data()});}
                std::cerr<<"Registration setup contract="<<contract(ownership)<<" pinned_bytes="<<registration_->bytes()<<" buffers_must_not_move=1\n";
            }
            const Frame& f=frames.front();size_t n=f.range.size();std::vector<float> d(n*3),o(n*3);
            for(size_t i=0;i<n;i++)for(size_t axis=0;axis<3;axis++) {d[axis*n+i]=f.direction[i*3+axis];o[axis*n+i]=f.offset[i*3+axis];}
            CU(cudaMalloc(&direction_,d.size()*4));CU(cudaMalloc(&offset_,o.size()*4));CU(cudaMalloc(&shifts_,f.shifts.size()*4));
            CU(cudaMemcpy(direction_,d.data(),d.size()*4,cudaMemcpyHostToDevice));CU(cudaMemcpy(offset_,o.data(),o.size()*4,cudaMemcpyHostToDevice));
            std::vector<int32_t> shifts=f.shifts;for(auto& s:shifts)s=(s+int32_t(width_))%int32_t(width_);
            CU(cudaMemcpy(shifts_,shifts.data(),shifts.size()*4,cudaMemcpyHostToDevice));
            if(graph) {
                // Parameters and mapped addresses are fixed; current frame's
                // timestamp/range limits are written into host_state_ each call.
                CU(cudaStreamBeginCapture(stream_,cudaStreamCaptureModeThreadLocal));launch(pointers(input_alias_,layout_),output_alias_);
                CU(cudaStreamEndCapture(stream_,&graph_handle_));CU(cudaGraphInstantiate(&graph_exec_,graph_handle_,nullptr,nullptr,0));
            }
        } catch(...) {release();throw;}
    }
    ~NextPlan(){release();}
    NextPlan(const NextPlan&)=delete;NextPlan& operator=(const NextPlan&)=delete;
    bool run(const Frame& f,std::vector<Point48>& output) {
        size_t n=size_t(width_)*height_;
        if(f.width!=width_||f.height!=height_||f.range.size()!=n||f.signal.size()!=n||f.reflectivity.size()!=n||f.near_ir.size()!=n||f.timestamps.size()!=width_||output.size()!=expected_size_||f.min_range>=f.max_range)
            throw std::runtime_error("Plan dimensions/output size/range bounds changed");
        if((ownership_==Ownership::RegisterOutput||ownership_==Ownership::RegisterAll)&&output.data()!=expected_output_)
            throw std::runtime_error("Registered output allocation changed");
        std::unique_ptr<Registration> temporary;
        try {
        Inputs in{};Point48* out=output_alias_;
        if(ownership_==Ownership::RegisterAll) {
            auto i=std::find(registered_frames_.begin(),registered_frames_.end(),&f);
            if(i==registered_frames_.end())throw std::runtime_error("Unregistered scan allocation");
            size_t index=i-registered_frames_.begin();in=registered_inputs_[index];const auto& host=registered_host_inputs_[index];
            if(host.range!=f.range.data()||host.signal!=f.signal.data()||host.reflectivity!=f.reflectivity.data()||host.near_ir!=f.near_ir.data()||host.timestamps!=f.timestamps.data())
                throw std::runtime_error("Registered scan allocation changed");
        } else if(ownership_==Ownership::RegisterEach) {
            auto spans=frame_spans(f);spans.push_back(span(output));temporary=std::make_unique<Registration>(spans);
            in=registered_inputs(f);out=device_alias(output.data());
        } else {
            size_t n=f.range.size();std::memcpy(host_input_+layout_.range,f.range.data(),n*4);std::memcpy(host_input_+layout_.signal,f.signal.data(),n*4);
            std::memcpy(host_input_+layout_.reflectivity,f.reflectivity.data(),n*2);std::memcpy(host_input_+layout_.near_ir,f.near_ir.data(),n*2);
            std::memcpy(host_input_+layout_.timestamps,f.timestamps.data(),size_t(width_)*8);in=pointers(input_alias_,layout_);
        }
        *host_state_={f.scan_ts,f.min_range,f.max_range};
        if(graph_)CU(cudaGraphLaunch(graph_exec_,stream_));else launch(in,out);
        CU(cudaEventRecord(ready_,stream_));CU(cudaEventSynchronize(ready_));
        if(ownership_==Ownership::Staged)std::memcpy(output.data(),host_output_,output.size()*48);
        bool dense=std::all_of(host_invalid_,host_invalid_+blocks_,[](uint32_t n){return n==0;});
        // Check unregistration failures and charge the per-frame cost.
        if(temporary)temporary->close();
        return dense;
        } catch(...) {cudaStreamSynchronize(stream_);throw;}
    }
};
#endif

struct NextMethod {std::string name,contract;std::function<bool(const Frame&,std::vector<Point48>&)> run;};
class Suite {
    Roi roi_;
    std::vector<std::array<float,3>> scratch_;
public:
    // Output and fixture allocations outlive every registered plan. Tests and
    // timed paths use this same allocation without resize/swap/move.
    std::vector<Point48> output;
    std::vector<NextMethod> methods;
#ifndef CLOUD_CPU_ONLY
    std::unique_ptr<GpuPlan> control;
    std::vector<std::unique_ptr<NextPlan>> plans;
#endif
    Suite(const std::vector<Frame>& frames,Roi roi,bool cpu_only,const std::set<std::string>& selected):
        roi_(roi),scratch_(frames.front().range.size()),output(size_t(frames.front().height)*roi.width) {
        auto wants=[&](const std::string& name){return selected.empty()||selected.count(name);};
        if(wants("cpu_fused"))methods.push_back({"cpu_fused","ordinary_host",[&](const Frame& f,std::vector<Point48>& o){return cpu_fused(f,roi_,o);}});
#ifndef CLOUD_CPU_ONLY
        if(!cpu_only) {
            if(wants("gpu_mapped_io")) {control=std::make_unique<GpuPlan>(frames.front(),roi,true);methods.push_back({"gpu_mapped_io","ordinary_host_copies",[&](const Frame& f,std::vector<Point48>& o){return control->run(f,o);}});}
            auto add=[&](std::string name,unsigned threads,bool tiled,bool graph,Ownership ownership) {
                if(!wants(name))return;auto begin=Clock::now();auto cpu=std::clock();
                auto plan=std::make_unique<NextPlan>(frames,roi,output,threads,tiled,graph,ownership);NextPlan* p=plan.get();
                std::cerr<<"Setup method="<<name<<" wall_ms="<<std::chrono::duration<double,std::milli>(Clock::now()-begin).count()<<" cpu_ms="<<1000.0*(std::clock()-cpu)/CLOCKS_PER_SEC<<'\n';
                plans.push_back(std::move(plan));methods.push_back({name,contract(ownership),[p](const Frame& f,std::vector<Point48>& o){return p->run(f,o);}});
            };
            add("gpu_soa_128",128,false,false,Ownership::Staged);add("gpu_soa_256",256,false,false,Ownership::Staged);add("gpu_soa_512",512,false,false,Ownership::Staged);
            add("gpu_tiled_256",256,true,false,Ownership::Staged);add("gpu_graph_256",256,false,true,Ownership::Staged);
            // Only one registration-based method may be resident in a suite:
            // otherwise CUDA would register the same output pages twice.
            unsigned registrations=unsigned(wants("gpu_registered_output"))+unsigned(wants("gpu_registered_all"))+unsigned(wants("gpu_register_each_frame"));
            if(registrations>1)throw std::runtime_error("Select at most one registration method per invocation with --methods");
            add("gpu_registered_output",256,false,false,Ownership::RegisterOutput);
            add("gpu_registered_all",256,false,false,Ownership::RegisterAll);
            add("gpu_register_each_frame",256,false,false,Ownership::RegisterEach);
        }
#endif
        if(methods.empty())throw std::runtime_error("No selected methods available");
        for(const auto& name:selected)if(std::none_of(methods.begin(),methods.end(),[&](const NextMethod& m){return m.name==name;}))throw std::runtime_error("Unknown/unavailable method "+name);
    }
    void validate(const std::vector<Frame>& frames) {
        for(const auto& f:frames) {std::vector<Point48> expected(output.size());bool dense=cpu_fused(f,roi_,expected);
            if(!f.reference.empty())compare(crop_reference(f,roi_),expected,f.name+" production reference");
            for(auto& m:methods) {std::memset(output.data(),0xa5,output.size()*sizeof(Point48));bool got=m.run(f,output);compare(expected,output,f.name+" "+m.name);if(got!=dense)throw std::runtime_error("Density mismatch "+m.name);}
        }
#ifndef CLOUD_CPU_ONLY
        for(auto& m:methods)if(m.name!="cpu_fused"&&m.name!="gpu_mapped_io") {
            std::vector<Point48> short_output(output.size()-1);bool rejected=false;
            try {m.run(frames.front(),short_output);}catch(const std::runtime_error&){rejected=true;}
            if(!rejected)throw std::runtime_error("Output bounds negative test failed");
            Frame malformed=frames.front();malformed.signal.pop_back();rejected=false;
            try {m.run(malformed,output);}catch(const std::runtime_error&){rejected=true;}
            if(!rejected)throw std::runtime_error("Input bounds negative test failed");
        }
        for(auto& m:methods)if(m.contract=="persistent_registered_output"||m.contract=="persistent_registered_scan_and_output") {
            std::vector<Point48> replacement(output.size());bool rejected=false;
            try {m.run(frames.front(),replacement);}catch(const std::runtime_error&){rejected=true;}
            if(!rejected)throw std::runtime_error("Registered output ownership negative test failed");
            if(m.contract=="persistent_registered_scan_and_output") {Frame other=frames.front();rejected=false;try {m.run(other,output);}catch(const std::runtime_error&){rejected=true;}if(!rejected)throw std::runtime_error("Registered input ownership negative test failed");}
        }
#endif
    }
};

#ifndef CLOUD_NEXT_LIBRARY
int main(int argc,char** argv) try {
    NextOptions next=next_options(argc,argv);Options& o=next.base;
#ifdef CLOUD_CPU_ONLY
    o.cpu_only=true;
#endif
    if(next.selected.empty()) {
        if(o.cpu_only)next.selected={"cpu_fused"};
        else next.selected={"cpu_fused","gpu_mapped_io","gpu_soa_128","gpu_soa_256","gpu_soa_512","gpu_tiled_256","gpu_graph_256"};
    }
    auto frames=o.synthetic?std::vector<Frame>{synthetic(2048,128,1),synthetic(2048,128,2),synthetic(2048,128,3),synthetic(2048,128,4)}:read_frames(o.input);
    if(o.synthetic)for(size_t i=1;i<frames.size();i++){frames[i].direction=frames[0].direction;frames[i].offset=frames[0].offset;}
    check_config(frames);const auto& first=frames.front();if(!o.roi.width)o.roi.width=first.width;
    if(o.roi.start>=first.width||!o.roi.width||o.roi.width>first.width)throw std::runtime_error("ROI outside global destaggered column grid");
#ifndef CLOUD_CPU_ONLY
    if(!o.cpu_only) {CU(cudaSetDeviceFlags(cudaDeviceMapHost|cudaDeviceScheduleBlockingSync));int id=0,supported=0;CU(cudaGetDevice(&id));cudaDeviceProp prop{};CU(cudaGetDeviceProperties(&prop,id));
        CU(cudaDeviceGetAttribute(&supported,cudaDevAttrHostRegisterSupported,id));
        std::cerr<<"GPU="<<prop.name<<" cc="<<prop.major<<'.'<<prop.minor<<" integrated="<<prop.integrated<<" hostRegisterSupported="<<supported<<" blocking_sync=1 period_ms="<<next.period_ms<<'\n';
        if(!prop.canMapHostMemory)throw std::runtime_error("GPU cannot map host memory");
        if(!supported&&(next.selected.count("gpu_registered_output")||next.selected.count("gpu_registered_all")||next.selected.count("gpu_register_each_frame")))throw std::runtime_error("Host registration unsupported");
    }
#endif
    // Keep the main suite in a separate scope: small fixtures may share heap
    // pages, so finish/unregister one suite before registering another.
    if(o.synthetic)for(auto shape:std::vector<std::pair<uint32_t,uint32_t>>{{17,5},{32,3},{1,1}}) {
        Frame f=synthetic(shape.first,shape.second,7);auto valid=f;valid.name="all_valid";std::fill(valid.range.begin(),valid.range.end(),1000);
        auto invalid=f;invalid.name="all_invalid";std::fill(invalid.range.begin(),invalid.range.end(),0);
        auto large=f;large.name="u32_conversion";large.max_range=UINT32_MAX;std::fill(large.range.begin(),large.range.end(),UINT32_MAX-1);std::fill(large.signal.begin(),large.signal.end(),UINT32_MAX);large.range[0]=UINT32_MAX;
        std::vector<Frame> small{std::move(f),std::move(valid),std::move(invalid),std::move(large)};
        Roi roi{shape.first>1?shape.first-2:0,shape.first};Suite suite(small,roi,o.cpu_only,next.selected);suite.validate(small);
    }
    Suite suite(frames,o.roi,o.cpu_only,next.selected);suite.validate(frames);
    std::cerr<<"Validation PASS: frames="<<frames.size()<<" methods="<<suite.methods.size()<<" width="<<o.roi.width<<" height="<<first.height<<" point_step=48 real="<<(first.real?1:0)<<'\n';
    if(o.validate_only)return 0;
    for(auto& m:suite.methods)for(unsigned i=0;i<o.warmup;i++)m.run(frames[i%frames.size()],suite.output);
    std::cout<<"source,crop_start,crop_width,height,round,order,implementation,contract,iterations,period_ms,wall_ms,cpu_ms,elapsed_ms,deadline_misses,checksum,is_dense\n"<<std::setprecision(12);
    for(unsigned round=0;round<o.rounds;round++)for(size_t order=0;order<suite.methods.size();order++) {
        auto& m=suite.methods[(order+round)%suite.methods.size()];bool dense=false;double wall_ms=0,cpu_ms=0;unsigned misses=0;
        auto begin=Clock::now(),due=begin;
        for(unsigned i=0;i<o.iterations;i++) {
            if(next.period_ms>0)std::this_thread::sleep_until(due);
            auto start=Clock::now();auto cpu=std::clock();dense=m.run(frames[i%frames.size()],suite.output);keep_result=suite.output[0].range;
            cpu_ms+=1000.0*(std::clock()-cpu)/CLOCKS_PER_SEC;auto end=Clock::now();wall_ms+=std::chrono::duration<double,std::milli>(end-start).count();
            if(next.period_ms>0) {due+=std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double,std::milli>(next.period_ms));if(end>due)misses++;}
        }
        double elapsed=std::chrono::duration<double,std::milli>(Clock::now()-begin).count();
        const auto& last=frames[(o.iterations-1)%frames.size()];std::vector<Point48> expected(suite.output.size());bool expected_dense=cpu_fused(last,o.roi,expected);
        compare(expected,suite.output,m.name+" timed result");if(dense!=expected_dense)throw std::runtime_error("Timed density mismatch");
        std::cout<<(first.real?"real":"synthetic")<<','<<o.roi.start<<','<<o.roi.width<<','<<first.height<<','<<round<<','<<order<<','<<m.name<<','<<m.contract<<','<<o.iterations<<','<<next.period_ms<<','<<wall_ms<<','<<cpu_ms<<','<<elapsed<<','<<misses<<','<<checksum(suite.output)<<','<<(dense?1:0)<<'\n';
    }
    if(!o.output.empty()){fs::create_directories(o.output);for(auto& m:suite.methods){m.run(frames.front(),suite.output);write_binary(fs::path(o.output)/(m.name+".cloud48"),suite.output.data(),suite.output.size());}}
    return 0;
}catch(const std::exception& e){std::cerr<<"ERROR: "<<e.what()<<'\n';return 1;}
#endif
