"""Short-lived ROS status publisher for a single direct CLI invocation."""
import os
import json
import sys
import time
import rclpy
from rclpy.duration import Duration
from std_msgs.msg import String

rclpy.init(args=[])
node = rclpy.create_node('so101_direct_episode_' + str(os.getpid()))
publishers = {arm:node.create_publisher(String, '/' + arm + '/episode_status', 10) for arm in ('arm2','arm3')}
try:
    until = time.monotonic() + 1
    while time.monotonic() < until:
        rclpy.spin_once(node, timeout_sec=.05)
    print('READY', flush=True)
    for line in sys.stdin:
        text=line.strip();arm=sys.argv[1]
        if text.startswith('{'):
            message=json.loads(text);arm=message['arm'];text=message['status']
        publisher=publishers[arm]
        publisher.publish(String(data=text))
        publisher.wait_for_all_acked(Duration(seconds=1))
finally:
    node.destroy_node()
    rclpy.try_shutdown()
