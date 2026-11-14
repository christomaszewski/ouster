#define PCL_NO_PRECOMPILE
#include "cloud_common.hpp"
#include <chrono>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <ouster/lidar_scan.h>
#include "point_cloud_compose.h"
#include "impl/cartesian.h"
using namespace cloud_test;
namespace core=ouster::sdk::core;
static volatile uint32_t sink=0;
struct ProductionInput {
    core::LidarScan scan;
    explicit ProductionInput(const Frame& f):scan(f.width,f.height,core::UDPProfileLidar::LEGACY) {
        auto r=scan.field<uint32_t>(core::ChanField::RANGE);std::copy(f.range.begin(),f.range.end(),r.data());
        auto s=scan.field<uint16_t>(core::ChanField::SIGNAL);auto n=scan.field<uint16_t>(core::ChanField::NEAR_IR);auto refl=scan.field<uint8_t>(core::ChanField::REFLECTIVITY);
        for(size_t i=0;i<f.range.size();i++){s.data()[i]=f.signal[i];n.data()[i]=f.near_ir[i];refl.data()[i]=f.reflectivity[i];}
        std::copy(f.timestamps.begin(),f.timestamps.end(),scan.timestamp().data());
    }
};
int main(int argc,char** argv) try {
    if(argc<2||argc>6){std::cerr<<"Usage: cpu_production_benchmark fixtures [iterations=64] [rounds=7] [crop_start=0] [crop_width=full]\n";return 2;}
    auto frames=read_frames(argv[1]);const auto& first=frames[0];unsigned iterations=argc>2?std::stoul(argv[2]):64,rounds=argc>3?std::stoul(argv[3]):7;
    Roi roi{argc>4?uint32_t(std::stoul(argv[4])):0,argc>5?uint32_t(std::stoul(argv[5])):first.width};
    if(!iterations||!rounds||!roi.width||roi.width>first.width||roi.start>=first.width)throw std::runtime_error("Invalid arguments");
    std::vector<ProductionInput> inputs;inputs.reserve(frames.size());for(const auto& f:frames){if(f.width!=first.width||f.height!=first.height||f.direction!=first.direction||f.offset!=first.offset||f.shifts!=first.shifts)throw std::runtime_error("Fixture calibrations differ");inputs.emplace_back(f);}
    const size_t n=first.range.size();core::ArrayX3R<float> direction(n,3),offset(n,3);std::copy(first.direction.begin(),first.direction.end(),direction.data());std::copy(first.offset.begin(),first.offset.end(),offset.data());
    core::PointCloudXYZf points(n,3);sensor_msgs::msg::PointCloud2 cloud;ouster_ros::PointCloud2Builder<ouster_ros::Point> writer(first.width,first.height);writer.attach(cloud);ouster_ros::Point_LEGACY staging;
    std::vector<int> shifts(first.shifts.begin(),first.shifts.end());std::vector<Point48> cropped(size_t(first.height)*roi.width);bool cropped_dense=true;
    auto run=[&](size_t index){auto& f=frames[index];auto& scan=inputs[index].scan;
        ouster::cartesianT(points,scan.field<uint32_t>(core::ChanField::RANGE),direction,offset,f.min_range,f.max_range,std::numeric_limits<float>::quiet_NaN());
        ouster_ros::scan_to_cloud_f<ouster_ros::Profile_LEGACY.size(),ouster_ros::Profile_LEGACY>(writer,staging,points,f.scan_ts,scan,shifts,true,true,1);
        if(roi.start||roi.width!=first.width){cropped_dense=true;const auto* bytes=cloud.data.data();for(uint32_t r=0;r<first.height;r++)for(uint32_t c=0;c<roi.width;c++){
            auto& point=cropped[size_t(r)*roi.width+c];std::memcpy(&point,bytes+(size_t(r)*first.width+(roi.start+c)%first.width)*48,48);
            cropped_dense&=!(std::isnan(point.x)||std::isnan(point.y)||std::isnan(point.z));
        }}
    };
    auto output=[&](){if(roi.start||roi.width!=first.width)return cropped;std::vector<Point48> out(n);std::memcpy(out.data(),cloud.data.data(),n*48);return out;};
    for(size_t i=0;i<frames.size();i++){run(i);compare(crop_reference(frames[i],roi),output(),"Actual production headers");}
    std::cerr<<"Actual production Cartesian/writer validation PASS; fixtures="<<frames.size()<<"\n";
    for(unsigned i=0;i<16;i++)run(i%frames.size());
    std::cout<<"source,crop_start,crop_width,height,round,order,implementation,iterations,wall_ms,cpu_ms,checksum,is_dense\n"<<std::setprecision(12);
    for(unsigned round=0;round<rounds;round++){auto wall=std::chrono::steady_clock::now();auto cpu=std::clock();
        for(unsigned i=0;i<iterations;i++){run(i%frames.size());sink=cloud.width;}
        double cpu_ms=1000.0*(std::clock()-cpu)/CLOCKS_PER_SEC;double wall_ms=std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-wall).count();
        auto out=output();compare(crop_reference(frames[(iterations-1)%frames.size()],roi),out,"Timed production result");
        std::cout<<"real,"<<roi.start<<','<<roi.width<<','<<first.height<<','<<round<<",0,"<<(roi.start||roi.width!=first.width?"cpu_production_full_then_crop":"cpu_production_headers")<<','<<iterations<<','<<wall_ms<<','<<cpu_ms<<','<<checksum(out)<<','<<((roi.start||roi.width!=first.width?cropped_dense:cloud.is_dense)?1:0)<<'\n';
    }return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
