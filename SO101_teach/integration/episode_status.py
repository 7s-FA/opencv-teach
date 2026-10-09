"""Short-lived ROS status publisher for a single direct CLI invocation."""
import os
import sys
import time
import rclpy
from rclpy.duration import Duration
from std_msgs.msg import String

rclpy.init(args=[])
node = rclpy.create_node('so101_direct_episode_' + str(os.getpid()))
publisher = node.create_publisher(String, '/' + sys.argv[1] + '/episode_status', 10)
try:
    until = time.monotonic() + 1
    while time.monotonic() < until:
        rclpy.spin_once(node, timeout_sec=.05)
    print('READY', flush=True)
    for line in sys.stdin:
        publisher.publish(String(data=line.strip()))
        publisher.wait_for_all_acked(Duration(seconds=1))
finally:
    node.destroy_node()
    rclpy.try_shutdown()
