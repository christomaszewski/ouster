# Backward-compatibility shim for `ament_target_dependencies`, which ROS 2 Lyrical removed from
# ament_cmake. Upstream ouster-ros (0.14.2 and current HEAD) still calls it, so it fails to configure
# on Lyrical with: Unknown CMake command "ament_target_dependencies".
#
# We inject this file via `-DCMAKE_PROJECT_ouster_ros_INCLUDE=` in docker/Dockerfile.runtime, so it is
# included by CMake immediately after `project(ouster_ros)` — the UPSTREAM SOURCES STAY BYTE-FOR-BYTE
# UNMODIFIED (nothing in src/ is patched). Delete this file and the cmake-arg once upstream supports
# Lyrical (track upstream CHANGELOG.rst / a future lyrical-* release tag).
#
# It reproduces the classic macro: for each ament package argument, attach that package's include
# dirs, compile definitions, and link libraries (preferring the modern imported target
# `<pkg>::<pkg>`, falling back to legacy <pkg>_TARGETS / <pkg>_LIBRARIES). Two details matter:
#   * target_link_libraries uses the PLAIN signature (no PUBLIC/PRIVATE keyword) — the real macro
#     did too, and upstream's own plain target_link_libraries() calls hit the SAME targets; CMake
#     forbids mixing keyword and plain signatures on one target.
#   * guards test for NON-EMPTY (not just DEFINED): on Lyrical some legacy *_INCLUDE_DIRS/_LIBRARIES
#     vars are defined-but-empty, and calling target_*() with no items is an error.
if(NOT COMMAND ament_target_dependencies)
  macro(ament_target_dependencies _att_target)
    foreach(_att_arg ${ARGN})
      if("${_att_arg}" MATCHES "^(PUBLIC|PRIVATE|INTERFACE|SYSTEM)$")
        # historical scope/markers — ignored (we use the plain link signature; see header)
      else()
        if(${_att_arg}_INCLUDE_DIRS)
          target_include_directories(${_att_target} SYSTEM PUBLIC ${${_att_arg}_INCLUDE_DIRS})
        endif()
        if(${_att_arg}_DEFINITIONS)
          target_compile_definitions(${_att_target} PUBLIC ${${_att_arg}_DEFINITIONS})
        endif()
        if(TARGET ${_att_arg}::${_att_arg})
          target_link_libraries(${_att_target} ${_att_arg}::${_att_arg})
        elseif(${_att_arg}_TARGETS OR ${_att_arg}_LIBRARIES)
          target_link_libraries(${_att_target} ${${_att_arg}_TARGETS} ${${_att_arg}_LIBRARIES})
        endif()
      endif()
    endforeach()
  endmacro()
endif()
