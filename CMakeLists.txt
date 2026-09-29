cmake_minimum_required(VERSION 3.0.2)
project(chassis_following)

find_package(catkin REQUIRED COMPONENTS
  rospy
  geometry_msgs
  nav_msgs
  std_msgs
  std_srvs
  actionlib
  actionlib_msgs
  navigation
  message_generation
)

catkin_python_setup()

add_action_files(FILES RunDistanceSweep.action)
add_service_files(FILES Capture.srv SetLight.srv)
generate_messages(DEPENDENCIES std_msgs actionlib_msgs)

catkin_package(CATKIN_DEPENDS
  message_runtime
  rospy
  std_msgs
  std_srvs
  geometry_msgs
  nav_msgs
  actionlib_msgs
  navigation
)

install(PROGRAMS
  scripts/chassis_following_node.py
  scripts/qvps_camera_node.py
  scripts/light_control_node.py
  scripts/run_sweep_client.py
  scripts/loop_nav.py
  scripts/config_ui.py
  scripts/run_charge.py
  scripts/run_cancel_charge.py
  scripts/manual_drive.py
  scripts/back_forward_test.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY launch config
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
)
