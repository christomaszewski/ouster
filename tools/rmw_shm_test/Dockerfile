# Optional profiling image. Supply the SAME immutable base for both control modes.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG CPU_GROUP_SHM=0
COPY librmw_zenoh_cpp.so /opt/ros/lyrical/lib/librmw_zenoh_cpp.so
ENV OUSTER_EXPERIMENT_CPU_GROUP_SHM=${CPU_GROUP_SHM}
LABEL org.ouster.study.rmw_source="ros2/rmw_zenoh@4e17ccfb9e114c87ac87b802e705b60e283b4115" \
      org.ouster.study.rmw_experiment="CPU-group direct SHM serialization; not fleet runtime"
