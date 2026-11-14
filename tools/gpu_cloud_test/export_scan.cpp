#define PCL_NO_PRECOMPILE
#include "cloud_common.hpp"
#include <iostream>
#include <iomanip>
#include <sstream>
#include <ouster/lidar_scan.h>
#include <ouster/packet.h>
#include <ouster/types.h>
#include <ouster/xyzlut.h>
#include "point_cloud_compose.h"
#include "impl/cartesian.h"

using namespace cloud_test;
namespace core=ouster::sdk::core;
template<class D,class S> std::vector<D> cast_field(const core::LidarScan& scan,const char* field) {
    auto x=scan.field<S>(field);std::vector<D> out(x.size());for(size_t i=0;i<out.size();i++)out[i]=static_cast<D>(x.data()[i]);return out;
}
int main(int argc,char** argv) try {
    if(argc<4||argc>7){std::cerr<<"Usage: export_scan metadata.json packets.len32 output_dir [max_frames=8] [min_range_mm=0] [max_range_mm=4294967295]\n";return 2;}
    std::ifstream metadata(argv[1]);std::string metadata_text((std::istreambuf_iterator<char>(metadata)),{});core::SensorInfo info(metadata_text);
    if(info.format.udp_profile_lidar!=core::UDPProfileLidar::LEGACY||info.num_returns()!=1)throw std::runtime_error("Prototype accepts single-return LEGACY only");
    unsigned max_frames=argc>4?std::stoul(argv[4]):8;uint32_t min_range=argc>5?std::stoul(argv[5]):0,max_range=argc>6?std::stoul(argv[6]):UINT32_MAX;
    const auto w=info.format.columns_per_frame,h=info.format.pixels_per_column;size_t n=size_t(w)*h;
    auto lut=core::make_xyz_lut(w,h,core::RANGE_UNIT,info.beam_to_lidar_transform,info.lidar_to_sensor_transform,info.beam_azimuth_angles,info.beam_altitude_angles);
    core::ArrayX3R<float> direction=lut.direction.cast<float>(),offset=lut.offset.cast<float>();
    core::PointCloudXYZf xyz_points(n,3);core::LidarScan scan(w,h,info.format.udp_profile_lidar);core::ScanBatcher batch(info);
    fs::create_directories(argv[3]);std::ifstream packets(argv[2],std::ios::binary);if(!packets)throw std::runtime_error("Cannot open packet records");
    size_t records=0,incomplete=0,complete=0;
    for(;;){uint8_t lb[4];packets.read(reinterpret_cast<char*>(lb),4);if(!packets){if(packets.eof()&&packets.gcount()==0)break;throw std::runtime_error("Truncated packet length");}
        uint32_t size=uint32_t(lb[0])|(uint32_t(lb[1])<<8)|(uint32_t(lb[2])<<16)|(uint32_t(lb[3])<<24);
        if(size!=batch.pf.lidar_packet_size)throw std::runtime_error("Unexpected packet length "+std::to_string(size));
        core::LidarPacket packet(size);packets.read(reinterpret_cast<char*>(packet.buf.data()),size);if(!packets)throw std::runtime_error("Truncated packet payload");
        records++;if(!batch(packet,scan))continue;
        if(!scan.complete({0,int(w)-1})){incomplete++;continue;}
        auto timestamps=scan.timestamp();const uint64_t scan_ts=timestamps[0];if(!scan_ts)throw std::runtime_error("Complete scan has zero column0 timestamp");
        std::ostringstream name;name<<"frame"<<std::setfill('0')<<std::setw(3)<<complete;fs::path dir=fs::path(argv[3])/name.str();fs::create_directories(dir);
        auto range=cast_field<uint32_t,uint32_t>(scan,core::ChanField::RANGE);
        auto signal=cast_field<uint32_t,uint16_t>(scan,core::ChanField::SIGNAL);
        auto reflec=cast_field<uint16_t,uint8_t>(scan,core::ChanField::REFLECTIVITY);
        auto nearir=cast_field<uint16_t,uint16_t>(scan,core::ChanField::NEAR_IR);
        write_binary(dir/"range.u32",range.data(),n);write_binary(dir/"signal.u32",signal.data(),n);write_binary(dir/"reflectivity.u16",reflec.data(),n);write_binary(dir/"near_ir.u16",nearir.data(),n);
        write_binary(dir/"timestamps.u64",timestamps.data(),w);
        std::vector<int32_t> shifts(info.format.pixel_shift_by_row.begin(),info.format.pixel_shift_by_row.end());write_binary(dir/"shifts.i32",shifts.data(),h);
        write_binary(dir/"direction.f32",direction.data(),n*3);write_binary(dir/"offset.f32",offset.data(),n*3);
        // Independent oracle invokes the actual production Cartesian and cloud
        // writer headers, including native->original field conversion.
        ouster::cartesianT(xyz_points,scan.field<uint32_t>(core::ChanField::RANGE),direction,offset,min_range,max_range,std::numeric_limits<float>::quiet_NaN());
        sensor_msgs::msg::PointCloud2 msg;ouster_ros::PointCloud2Builder<ouster_ros::Point> builder(w,h);builder.attach(msg);ouster_ros::Point_LEGACY staging;
        ouster_ros::scan_to_cloud_f<ouster_ros::Profile_LEGACY.size(),ouster_ros::Profile_LEGACY>(builder,staging,xyz_points,scan_ts,scan,info.format.pixel_shift_by_row,true,true,1);
        if(msg.point_step!=48||msg.width!=w||msg.height!=h||msg.data.size()!=n*48)throw std::runtime_error("Unexpected ROS cloud layout");
        write_binary(dir/"reference.cloud48",msg.data.data(),msg.data.size());
        std::ofstream manifest(dir/"manifest.txt");manifest<<"schema=ouster-gpu-scan-v1\nsource=real_packet_capture\nwidth="<<w<<"\nheight="<<h<<"\nscan_ts="<<scan_ts<<"\nmin_range="<<min_range<<"\nmax_range="<<max_range<<"\nframe_id="<<scan.frame_id<<"\nframe=os_sensor\ndestagger=1\nmask=none\nmath=fma_float32\n";
        manifest.close();auto fixture=read_frame(dir);std::vector<Point48> check(n);cpu_fused(fixture,{0,w},check);compare(fixture.reference,check,"exported production oracle");
        std::cout<<"{\"frame\":\""<<name.str()<<"\",\"scan_ts\":"<<scan_ts<<",\"is_dense\":"<<(msg.is_dense?"true":"false")<<",\"reference_checksum\":"<<checksum(check)<<"}\n";
        if(++complete>=max_frames)break;
    }
    if(!complete)throw std::runtime_error("No complete scan exported");
    std::cout<<"{\"packets_read\":"<<records<<",\"incomplete_scans_skipped\":"<<incomplete<<",\"complete_scans_exported\":"<<complete<<"}\n";
    return 0;
}catch(const std::exception& e){std::cerr<<e.what()<<'\n';return 1;}
