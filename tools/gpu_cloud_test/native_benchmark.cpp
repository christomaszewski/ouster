#define CLOUD_CPU_ONLY
#define CLOUD_BENCHMARK_LIBRARY
#include "cloud_benchmark.cu"
#include "native_backend.h"
#include <thread>

struct NativeStorage {
    std::vector<uint16_t> signal;std::vector<uint8_t> reflectivity;
    explicit NativeStorage(const Frame& f):signal(f.signal.size()),reflectivity(f.reflectivity.size()) {
        for(size_t i=0;i<signal.size();i++) {if(f.signal[i]>UINT16_MAX||f.reflectivity[i]>UINT8_MAX)throw std::runtime_error("Fixture cannot represent native LEGACY signal/reflectivity");signal[i]=uint16_t(f.signal[i]);reflectivity[i]=uint8_t(f.reflectivity[i]);}
    }
    ouster_gpu_scan scan(const Frame& f) const {return {f.range.data(),signal.data(),reflectivity.data(),f.near_ir.data(),f.timestamps.data(),f.scan_ts,f.range.size(),f.timestamps.size()};}
};
static void api(int status,const char* error) {if(status)throw std::runtime_error(std::string("GPU ABI failure: ")+error);}
struct NativeOptions {Options base;uint32_t mode=OUSTER_GPU_REGISTERED_OUTPUT;double period=0;std::string trace;};
NativeOptions parse_native(int argc,char** argv) {
    NativeOptions n;std::vector<char*> base{argv[0]};
    for(int i=1;i<argc;i++) {std::string a=argv[i];
        if(a=="--mode"||a=="--period-ms"||a=="--trace-csv") {if(++i>=argc)throw std::runtime_error("Missing argument for "+a);std::string v=argv[i];
            if(a=="--mode") {if(v=="staged")n.mode=OUSTER_GPU_STAGED;else if(v=="registered-output")n.mode=OUSTER_GPU_REGISTERED_OUTPUT;else if(v=="registered-all")n.mode=OUSTER_GPU_REGISTERED_ALL;else throw std::runtime_error("Invalid native mode");}
            else if(a=="--period-ms")n.period=std::stod(v);else n.trace=v;
        } else base.push_back(argv[i]);
    }
    n.base=options(int(base.size()),base.data());if(!std::isfinite(n.period)||n.period<0||n.period>1000||n.base.warmup>100000||(!n.trace.empty()&&size_t(n.base.iterations)*n.base.rounds*2>1000000))throw std::runtime_error("Invalid native benchmark bounds");return n;
}
const char* native_name(uint32_t mode) {return mode==OUSTER_GPU_STAGED?"gpu_native_staged":mode==OUSTER_GPU_REGISTERED_OUTPUT?"gpu_native_registered_output":"gpu_native_registered_all";}
bool native_cpu(const Frame& f,const NativeStorage& native,Roi roi,std::vector<uint8_t>& out) {
    bool dense=true;for(uint32_t row=0;row<f.height;row++) {int32_t shift=(f.shifts[row]+int32_t(f.width))%int32_t(f.width);
        for(uint32_t col=0;col<roi.width;col++) {uint32_t global=roi.start+col;if(global>=f.width)global-=f.width;int32_t s=int32_t(global)-shift;if(s<0)s+=f.width;size_t src=size_t(row)*f.width+uint32_t(s);auto xyz_value=xyz(f,src);
            Point48 p{};p.x=xyz_value[0];p.y=xyz_value[1];p.z=xyz_value[2];p.homogeneous=1;p.intensity=float(native.signal[src]);uint64_t ts=f.timestamps[s];p.t=uint32_t(ts>f.scan_ts?ts-f.scan_ts:0);p.reflectivity=native.reflectivity[src];p.ring=uint16_t(row);p.ambient=f.near_ir[src];p.range=f.range[src];
            dense&=!(std::isnan(p.x)||std::isnan(p.y)||std::isnan(p.z));std::memcpy(out.data()+(size_t(row)*roi.width+col)*48,&p,48);
        }
    }return dense;
}
static uint64_t byte_checksum(const std::vector<uint8_t>& bytes) {uint64_t h=1469598103934665603ULL;for(auto x:bytes){h^=x;h*=1099511628211ULL;}return h;}
static void compare_bytes(const std::vector<Point48>& expected,const uint8_t* actual,size_t bytes,const std::string& name) {
    if(bytes!=expected.size()*48)throw std::runtime_error(name+" byte size mismatch");const auto* e=reinterpret_cast<const uint8_t*>(expected.data());
    for(size_t i=0;i<bytes;i++)if(e[i]!=actual[i])throw std::runtime_error(name+" mismatch point="+std::to_string(i/48)+" byte="+std::to_string(i%48));
}
class NativeSuite {
    const std::vector<Frame>& frames_;Roi roi_;uint32_t mode_;bool cpu_only_;ouster_gpu_handle* handle_=nullptr;
public:
    std::vector<NativeStorage> native;std::vector<ouster_gpu_scan> scans;std::vector<uint8_t> output;
    NativeSuite(const std::vector<Frame>& frames,Roi roi,uint32_t mode,bool cpu_only):frames_(frames),roi_(roi),mode_(mode),cpu_only_(cpu_only),output(size_t(frames.front().height)*roi.width*48) {
        native.reserve(frames.size());for(const auto& f:frames)native.emplace_back(f);for(size_t i=0;i<frames.size();i++)scans.push_back(native[i].scan(frames[i]));
        if(cpu_only)return;const auto& f=frames.front();ouster_gpu_config c{OUSTER_GPU_ABI_VERSION,sizeof(ouster_gpu_config),f.width,f.height,roi.start,roi.width,f.min_range,f.max_range,mode,f.direction.data(),f.offset.data(),f.shifts.data()};char error[512]{};
        auto start=Clock::now();auto cpu=std::clock();api(ouster_gpu_create(&c,&handle_,error,sizeof(error)),error);
        try {prepare(output.data(),output.size());}catch(...){ouster_gpu_destroy(handle_);handle_=nullptr;throw;}
        std::cerr<<"Native setup mode="<<native_name(mode)<<" wall_ms="<<std::chrono::duration<double,std::milli>(Clock::now()-start).count()<<" cpu_ms="<<1000.0*(std::clock()-cpu)/CLOCKS_PER_SEC<<" input_bytes_per_frame="<<f.range.size()*9+f.width*8<<" output_bytes="<<output.size()<<'\n';
    }
    ~NativeSuite(){ouster_gpu_destroy(handle_);}
    void prepare(uint8_t* ptr,size_t bytes) {char e[512]{};api(ouster_gpu_prepare(handle_,ptr,bytes,mode_==OUSTER_GPU_REGISTERED_ALL?scans.data():nullptr,mode_==OUSTER_GPU_REGISTERED_ALL?scans.size():0,e,sizeof(e)),e);}
    void release() {char e[512]{};api(ouster_gpu_release(handle_,e,sizeof(e)),e);}
    bool run(size_t index,bool gpu) {if(!gpu)return native_cpu(frames_[index],native[index],roi_,output);int dense=0;char e[512]{};api(ouster_gpu_run(handle_,&scans[index],output.data(),output.size(),&dense,e,sizeof(e)),e);return dense!=0;}
    void validate() {
        for(size_t i=0;i<frames_.size();i++) {const auto& f=frames_[i];std::vector<Point48> expected(output.size()/48);bool density=cpu_fused(f,roi_,expected);
            if(!f.reference.empty())compare(crop_reference(f,roi_),expected,f.name+" production reference");
            for(int gpu=0;gpu<=(cpu_only_?0:1);gpu++) {std::fill(output.begin(),output.end(),0xa5);if(run(i,gpu)!=density)throw std::runtime_error("Native density mismatch");compare_bytes(expected,output.data(),output.size(),f.name+(gpu?" native GPU":" native CPU"));}
        }
        if(cpu_only_)return;
        char e[512]{};int dense=0;auto bad=scans.front();bad.pixels--;
        if(!ouster_gpu_run(handle_,&bad,output.data(),output.size(),&dense,e,sizeof(e)))throw std::runtime_error("Malformed native scan accepted");
        if(!ouster_gpu_run(handle_,&scans.front(),output.data(),output.size()-1,&dense,e,sizeof(e)))throw std::runtime_error("Short native output accepted");
        bad=scans.front();bad.timestamps=nullptr;if(!ouster_gpu_run(handle_,&bad,output.data(),output.size(),&dense,e,sizeof(e)))throw std::runtime_error("Null timestamp pointer accepted");
        if(mode_!=OUSTER_GPU_STAGED) {
            std::vector<uint8_t> replacement(output.size());if(!ouster_gpu_run(handle_,&scans.front(),replacement.data(),replacement.size(),&dense,e,sizeof(e)))throw std::runtime_error("Unregistered replacement output accepted");
        }
        // Exercise vector<uint8_t> storage, checked direct-output alignment,
        // registration release/replacement and retained old bytes.
        run(0,true);const auto retained=output;release();std::vector<uint8_t> shifted(output.size()+1,0xa5);
        if(mode_==OUSTER_GPU_STAGED) {
            prepare(shifted.data()+1,output.size());api(ouster_gpu_run(handle_,&scans.front(),shifted.data()+1,output.size(),&dense,e,sizeof(e)),e);
            if(!std::equal(retained.begin(),retained.end(),shifted.begin()+1)||shifted.front()!=0xa5)throw std::runtime_error("Staged byte alignment mismatch");
        } else {
            if(!ouster_gpu_prepare(handle_,shifted.data()+1,output.size(),mode_==OUSTER_GPU_REGISTERED_ALL?scans.data():nullptr,mode_==OUSTER_GPU_REGISTERED_ALL?scans.size():0,e,sizeof(e)))throw std::runtime_error("Misaligned registered output accepted");
            prepare(shifted.data(),output.size());api(ouster_gpu_run(handle_,&scans.front(),shifted.data(),output.size(),&dense,e,sizeof(e)),e);
            if(!std::equal(retained.begin(),retained.end(),shifted.begin()))throw std::runtime_error("Replacement native output mismatch");
        }
        if(output!=retained)throw std::runtime_error("Retained native output changed");
        release();prepare(output.data(),output.size());
        // A prepared stable pool must reject a different native field allocation.
        if(mode_==OUSTER_GPU_REGISTERED_ALL) {auto replacement=native.front().signal;bad=scans.front();bad.signal=replacement.data();if(!ouster_gpu_run(handle_,&bad,output.data(),output.size(),&dense,e,sizeof(e)))throw std::runtime_error("Replacement native scan accepted");}
    }
};
void native_synthetic(Frame& f) {for(auto& x:f.signal)x&=UINT16_MAX;for(auto& x:f.reflectivity)x&=UINT8_MAX;}
struct Trace {unsigned round,order,method,iteration;double wall,cpu,start_late,completion_late;};
int main(int argc,char** argv) try {
    auto n=parse_native(argc,argv);auto& o=n.base;auto frames=o.synthetic?std::vector<Frame>{synthetic(2048,128,1),synthetic(2048,128,2),synthetic(2048,128,3),synthetic(2048,128,4)}:read_frames(o.input);
    if(o.synthetic)for(auto& f:frames){native_synthetic(f);f.direction=frames.front().direction;f.offset=frames.front().offset;}
    check_config(frames);for(const auto& f:frames)if(f.min_range!=frames.front().min_range||f.max_range!=frames.front().max_range)throw std::runtime_error("Native group range limits differ");
    if(!o.roi.width)o.roi.width=frames.front().width;if(o.roi.start>=frames.front().width||!o.roi.width||o.roi.width>frames.front().width)throw std::runtime_error("Invalid native crop");
    if(o.synthetic)for(auto shape:std::vector<std::pair<uint32_t,uint32_t>>{{17,5},{32,3},{1,1}})for(int kind=0;kind<4;kind++) {
        Frame f=synthetic(shape.first,shape.second,7);native_synthetic(f);if(kind==1)std::fill(f.range.begin(),f.range.end(),1000);if(kind==2)std::fill(f.range.begin(),f.range.end(),0);if(kind==3){f.max_range=UINT32_MAX;std::fill(f.range.begin(),f.range.end(),UINT32_MAX-1);f.range[0]=UINT32_MAX;std::fill(f.signal.begin(),f.signal.end(),UINT16_MAX);std::fill(f.reflectivity.begin(),f.reflectivity.end(),UINT8_MAX);}
        std::vector<Frame> small{std::move(f)};Roi roi{shape.first>1?shape.first-2:0,shape.first};NativeSuite s(small,roi,n.mode,o.cpu_only);s.validate();
    }
    NativeSuite suite(frames,o.roi,n.mode,o.cpu_only);suite.validate();std::cerr<<"Native validation PASS: frames="<<frames.size()<<" width="<<o.roi.width<<" height="<<frames.front().height<<" signal=u16 reflectivity=u8 range=u32 near_ir=u16 byte_output=1\n";
    if(o.validate_only)return 0;unsigned methods=o.cpu_only?1:2;
    for(unsigned m=0;m<methods;m++)for(unsigned i=0;i<o.warmup;i++)suite.run(i%frames.size(),m);
    std::vector<Trace> traces;if(!n.trace.empty())traces.reserve(size_t(o.iterations)*o.rounds*methods);
    std::cout<<"source,crop_start,crop_width,height,round,order,implementation,iterations,period_ms,wall_ms,cpu_ms,elapsed_ms,deadline_misses,max_wall_ms,max_start_lateness_ms,max_completion_lateness_ms,checksum,is_dense\n"<<std::setprecision(12);
    for(unsigned round=0;round<o.rounds;round++)for(unsigned order=0;order<methods;order++) {
        unsigned method=(round+order)%methods;double wall=0,cpu=0,max_wall=0,max_start=0,max_late=0;unsigned misses=0;bool dense=false;auto begin=Clock::now(),due=begin;
        for(unsigned i=0;i<o.iterations;i++) {
            if(n.period>0)std::this_thread::sleep_until(due);auto start=Clock::now();auto c=std::clock();dense=suite.run(i%frames.size(),method);keep_result=suite.output[0];double used=1000.0*(std::clock()-c)/CLOCKS_PER_SEC;auto end=Clock::now();double duration=std::chrono::duration<double,std::milli>(end-start).count();wall+=duration;cpu+=used;max_wall=std::max(max_wall,duration);
            double start_late=0,late=0;if(n.period>0){start_late=std::max(0.0,std::chrono::duration<double,std::milli>(start-due).count());due+=std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double,std::milli>(n.period));late=std::max(0.0,std::chrono::duration<double,std::milli>(end-due).count());if(late>0)misses++;max_start=std::max(max_start,start_late);max_late=std::max(max_late,late);}
            if(!n.trace.empty())traces.push_back({round,order,method,i,duration,used,start_late,late});
        }
        double elapsed=std::chrono::duration<double,std::milli>(Clock::now()-begin).count();const auto& last=frames[(o.iterations-1)%frames.size()];std::vector<Point48> expected(suite.output.size()/48);bool d=cpu_fused(last,o.roi,expected);compare_bytes(expected,suite.output.data(),suite.output.size(),"native timed result");if(dense!=d)throw std::runtime_error("Native timed density mismatch");
        std::cout<<(frames.front().real?"real":"synthetic")<<','<<o.roi.start<<','<<o.roi.width<<','<<frames.front().height<<','<<round<<','<<order<<','<<(method?native_name(n.mode):"cpu_native_fused")<<','<<o.iterations<<','<<n.period<<','<<wall<<','<<cpu<<','<<elapsed<<','<<misses<<','<<max_wall<<','<<max_start<<','<<max_late<<','<<byte_checksum(suite.output)<<','<<(dense?1:0)<<'\n';
    }
    if(!n.trace.empty()){std::ofstream out(n.trace);if(!out)throw std::runtime_error("Cannot open native trace");out<<"round,order,implementation,iteration,wall_ms,cpu_ms,start_lateness_ms,completion_lateness_ms\n"<<std::setprecision(12);for(auto x:traces)out<<x.round<<','<<x.order<<','<<(x.method?native_name(n.mode):"cpu_native_fused")<<','<<x.iteration<<','<<x.wall<<','<<x.cpu<<','<<x.start_late<<','<<x.completion_late<<'\n';}
    if(!o.output.empty()){fs::create_directories(o.output);for(unsigned m=0;m<methods;m++){suite.run(0,m);write_binary(fs::path(o.output)/(std::string(m?native_name(n.mode):"cpu_native_fused")+".cloud48"),suite.output.data(),suite.output.size());}}
    return 0;
}catch(const std::exception& e){std::cerr<<"ERROR: "<<e.what()<<'\n';return 1;}
