#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace cloud_test {
namespace fs = std::filesystem;
struct alignas(16) Point48 {
    float x, y, z, homogeneous;
    float intensity;
    uint32_t t;
    uint16_t reflectivity, ring, ambient, padding30;
    uint32_t range, padding36, padding40, padding44;
};
static_assert(sizeof(Point48)==48 && offsetof(Point48,range)==32 &&
              offsetof(Point48,ambient)==28 && offsetof(Point48,reflectivity)==24);

struct Frame {
    uint32_t width=0, height=0, min_range=0, max_range=UINT32_MAX;
    uint64_t scan_ts=0;
    std::string name;
    bool real=false;
    std::vector<uint32_t> range, signal;
    std::vector<uint16_t> reflectivity, near_ir;
    std::vector<uint64_t> timestamps;
    std::vector<int32_t> shifts;
    std::vector<float> direction, offset;
    std::vector<Point48> reference;
};
struct Roi { uint32_t start=0, width=0; };
inline float canonical_nan() { uint32_t v=0x7fc00000; float f; std::memcpy(&f,&v,4); return f; }
inline uint32_t bits(float f) { uint32_t v; std::memcpy(&v,&f,4); return v; }

template<class T> std::vector<T> read_binary(const fs::path& path,size_t count) {
    if(fs::file_size(path)!=count*sizeof(T)) throw std::runtime_error("Wrong binary size: "+path.string());
    std::vector<T> v(count); std::ifstream f(path,std::ios::binary);
    f.read(reinterpret_cast<char*>(v.data()),v.size()*sizeof(T));
    if(!f) throw std::runtime_error("Cannot read "+path.string()); return v;
}
template<class T> void write_binary(const fs::path& path,const T* data,size_t count) {
    std::ofstream f(path,std::ios::binary); f.write(reinterpret_cast<const char*>(data),count*sizeof(T));
    if(!f) throw std::runtime_error("Cannot write "+path.string());
}
inline void validate_frame(const Frame& f) {
    const size_t n=size_t(f.width)*f.height;
    if(!f.width || !f.height || f.height>65536 || n>16777216 || f.min_range>=f.max_range)
        throw std::runtime_error("Invalid fixture dimensions/range bounds");
    if(f.range.size()!=n||f.signal.size()!=n||f.reflectivity.size()!=n||f.near_ir.size()!=n||
       f.timestamps.size()!=f.width||f.shifts.size()!=f.height||f.direction.size()!=n*3||f.offset.size()!=n*3)
        throw std::runtime_error("Fixture array sizes disagree");
    for(auto s:f.shifts) if(s < -int64_t(f.width)||s >= int64_t(f.width)) throw std::runtime_error("Invalid pixel shift");
}
inline Frame read_frame(const fs::path& path) {
    std::ifstream in(path/"manifest.txt"); std::map<std::string,std::string> kv; std::string line;
    while(std::getline(in,line)) {auto p=line.find('=');if(p!=std::string::npos)kv[line.substr(0,p)]=line.substr(p+1);}
    if(kv["schema"]!="ouster-gpu-scan-v1") throw std::runtime_error("Unknown fixture schema: "+path.string());
    Frame f; f.name=path.filename().string();f.width=std::stoul(kv.at("width"));f.height=std::stoul(kv.at("height"));
    f.scan_ts=std::stoull(kv.at("scan_ts"));f.min_range=std::stoul(kv.at("min_range"));f.max_range=std::stoul(kv.at("max_range"));f.real=kv.at("source")=="real_packet_capture";
    size_t n=size_t(f.width)*f.height;
    if(!n||n>16777216)throw std::runtime_error("Oversized fixture");
    f.range=read_binary<uint32_t>(path/"range.u32",n);f.signal=read_binary<uint32_t>(path/"signal.u32",n);
    f.reflectivity=read_binary<uint16_t>(path/"reflectivity.u16",n);f.near_ir=read_binary<uint16_t>(path/"near_ir.u16",n);
    f.timestamps=read_binary<uint64_t>(path/"timestamps.u64",f.width);f.shifts=read_binary<int32_t>(path/"shifts.i32",f.height);
    f.direction=read_binary<float>(path/"direction.f32",n*3);f.offset=read_binary<float>(path/"offset.f32",n*3);
    if(fs::exists(path/"reference.cloud48"))f.reference=read_binary<Point48>(path/"reference.cloud48",n);
    if(f.real&&f.reference.empty())throw std::runtime_error("Real fixture requires independent ROS reference.cloud48");
    validate_frame(f);return f;
}
inline std::vector<Frame> read_frames(const fs::path& path) {
    if(fs::exists(path/"manifest.txt"))return {read_frame(path)};
    std::vector<fs::path> dirs;for(auto& e:fs::directory_iterator(path))if(e.is_directory()&&fs::exists(e.path()/"manifest.txt"))dirs.push_back(e.path());
    std::sort(dirs.begin(),dirs.end());std::vector<Frame> frames;for(auto& d:dirs)frames.push_back(read_frame(d));
    if(frames.empty())throw std::runtime_error("No fixtures");return frames;
}
inline uint32_t source_column(const Frame& f,uint32_t row,uint32_t target) {
    int32_t shift=(f.shifts[row]+int32_t(f.width))%int32_t(f.width);
    int32_t column=int32_t(target)-shift;if(column<0)column+=f.width;return uint32_t(column);
}
inline std::array<float,3> xyz(const Frame& f,size_t i) {
    uint32_t r=f.range[i];if(r<=f.min_range||r>=f.max_range)return {canonical_nan(),canonical_nan(),canonical_nan()};
    // Current ARM64 production cartesianT emits fmadd for these expressions.
    float rf=static_cast<float>(r);return {std::fma(rf,f.direction[3*i],f.offset[3*i]),std::fma(rf,f.direction[3*i+1],f.offset[3*i+1]),std::fma(rf,f.direction[3*i+2],f.offset[3*i+2])};
}
inline Point48 pack(const Frame& f,size_t src,uint32_t row,uint32_t column,const std::array<float,3>& p) {
    Point48 q{};q.x=p[0];q.y=p[1];q.z=p[2];q.homogeneous=1.0f;q.intensity=static_cast<float>(f.signal[src]);
    q.t=static_cast<uint32_t>(f.timestamps[column]>f.scan_ts?f.timestamps[column]-f.scan_ts:0);
    q.reflectivity=f.reflectivity[src];q.ring=static_cast<uint16_t>(row);q.ambient=f.near_ir[src];q.range=f.range[src];return q;
}
inline bool cpu_staged(const Frame& f,Roi roi,std::vector<std::array<float,3>>& scratch,std::vector<Point48>& output) {
    for(size_t i=0;i<f.range.size();i++)scratch[i]=xyz(f,i);
    bool dense=true;
    for(uint32_t row=0;row<f.height;row++){
      const int32_t shift=(f.shifts[row]+int32_t(f.width))%int32_t(f.width);
      for(uint32_t col=0;col<roi.width;col++){
        uint32_t global=roi.start+col;if(global>=f.width)global-=f.width;
        int32_t source=int32_t(global)-shift;if(source<0)source+=f.width;
        uint32_t s=uint32_t(source);size_t i=size_t(row)*f.width+s;
        auto p=scratch[i];dense&=!(std::isnan(p[0])||std::isnan(p[1])||std::isnan(p[2]));output[size_t(row)*roi.width+col]=pack(f,i,row,s,p);
    }}return dense;
}
inline bool cpu_fused(const Frame& f,Roi roi,std::vector<Point48>& output) {
    bool dense=true;
    for(uint32_t row=0;row<f.height;row++){
      const int32_t shift=(f.shifts[row]+int32_t(f.width))%int32_t(f.width);
      for(uint32_t col=0;col<roi.width;col++){
        uint32_t global=roi.start+col;if(global>=f.width)global-=f.width;
        int32_t source=int32_t(global)-shift;if(source<0)source+=f.width;
        uint32_t s=uint32_t(source);size_t i=size_t(row)*f.width+s;auto p=xyz(f,i);
        dense&=!(std::isnan(p[0])||std::isnan(p[1])||std::isnan(p[2]));output[size_t(row)*roi.width+col]=pack(f,i,row,s,p);
    }}return dense;
}
inline void compare(const std::vector<Point48>& a,const std::vector<Point48>& b,const std::string& label) {
    if(a.size()!=b.size())throw std::runtime_error(label+": cloud size mismatch");
    for(size_t i=0;i<a.size();i++)if(std::memcmp(&a[i],&b[i],48)!=0) {
        // Canonical invalid ranges should match bitwise too. Do not silently
        // accept approximate XYZ or changed NaN payloads in this prototype.
        const auto* ap=reinterpret_cast<const uint8_t*>(&a[i]);const auto* bp=reinterpret_cast<const uint8_t*>(&b[i]);
        for(size_t k=0;k<48;k++)if(ap[k]!=bp[k])throw std::runtime_error(label+": mismatch point="+std::to_string(i)+" byte="+std::to_string(k)+" expected="+std::to_string(ap[k])+" actual="+std::to_string(bp[k]));
    }
}
inline uint64_t checksum(const std::vector<Point48>& a) {
    uint64_t h=1469598103934665603ULL;const auto* p=reinterpret_cast<const uint8_t*>(a.data());
    for(size_t i=0;i<a.size()*48;i++){h^=p[i];h*=1099511628211ULL;}return h;
}
inline std::vector<Point48> crop_reference(const Frame& f,Roi roi) {
    std::vector<Point48> out(size_t(f.height)*roi.width);
    for(uint32_t r=0;r<f.height;r++)for(uint32_t c=0;c<roi.width;c++)out[size_t(r)*roi.width+c]=f.reference[size_t(r)*f.width+(roi.start+c)%f.width];return out;
}
inline Frame synthetic(uint32_t width,uint32_t height,unsigned seed) {
    Frame f;f.width=width;f.height=height;f.name="synthetic"+std::to_string(seed);f.scan_ts=10000000000ULL;f.min_range=100;f.max_range=100000;
    size_t n=size_t(width)*height;f.range.resize(n);f.signal.resize(n);f.reflectivity.resize(n);f.near_ir.resize(n);f.direction.resize(n*3);f.offset.resize(n*3);f.timestamps.resize(width);f.shifts.resize(height);
    for(uint32_t c=0;c<width;c++)f.timestamps[c]=(c%7==0)?f.scan_ts-1:f.scan_ts+uint64_t(c)*1000000000ULL;
    for(uint32_t r=0;r<height;r++)f.shifts[r]=r%3==0?-int32_t(width):r%3==1?int32_t(width)-1:-int32_t(width)/2;
    for(size_t i=0;i<n;i++){
        f.range[i]=(uint64_t(i)*7919+seed*17)%110000;if(i%17==0)f.range[i]=f.min_range;if(i%19==0)f.range[i]=f.max_range;
        f.signal[i]=uint32_t(i*1871+seed);f.reflectivity[i]=uint16_t(i*977);f.near_ir[i]=uint16_t(i*1031);
        for(int j=0;j<3;j++){f.direction[i*3+j]=float(int((i*7+j*131+seed)%2000)-1000)/1000000.0f;f.offset[i*3+j]=float(int((i+j*9)%13)-6)/1000.0f;}
    }validate_frame(f);return f;
}
} // namespace cloud_test
