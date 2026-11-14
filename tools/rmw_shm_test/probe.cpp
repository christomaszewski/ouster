#include <rclcpp/rclcpp.hpp>
#include <rmw/rmw.h>
#include <sensor_msgs/msg/point_cloud2.hpp>
#include <chrono>
#include <iostream>
#include <thread>

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);
    rmw_set_log_severity(RMW_LOG_SEVERITY_DEBUG);
    auto node = std::make_shared<rclcpp::Node>("shm_probe_pub");
    auto pub = node->create_publisher<sensor_msgs::msg::PointCloud2>(
        "/shm_probe", rclcpp::QoS(10).reliable());
    std::cout << "can_loan_messages=" << pub->can_loan_messages() << std::endl;
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(15);
    while (pub->get_subscription_count() < 1) {
        if (std::chrono::steady_clock::now() > deadline) return 2;
        rclcpp::spin_some(node);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    sensor_msgs::msg::PointCloud2 cloud;
    cloud.header.frame_id = "probe";
    cloud.width = 2048; cloud.height = 128;
    cloud.point_step = 48; cloud.row_step = cloud.width * cloud.point_step;
    cloud.is_bigendian = false; cloud.is_dense = false;
    for (const auto& name : {"x", "y", "z"}) {
        sensor_msgs::msg::PointField field;
        field.name = name; field.offset = cloud.fields.size() * 4;
        field.datatype = 7; field.count = 1;
        cloud.fields.push_back(field);
    }
    cloud.data.resize(static_cast<size_t>(cloud.row_step) * cloud.height);
    for (size_t i = 0; i < cloud.data.size(); ++i) cloud.data[i] = (i * 31 + 7) % 256;
    for (int i = 0; i < 40; ++i) {
        cloud.header.stamp.sec = i + 1;
        pub->publish(cloud);
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    rclcpp::shutdown();
}
