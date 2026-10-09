"""Simple ROS 2 String topics. No motor/device access in this process."""
import json,queue,sys,threading
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile,ReliabilityPolicy,DurabilityPolicy
from std_msgs.msg import String


class EpisodeTopics(Node):
    def __init__(self):
        super().__init__('so101_episode_receiver');self.responses=queue.Queue(100);self.pipe_closed=False
        qos=QoSProfile(depth=10,reliability=ReliabilityPolicy.RELIABLE,durability=DurabilityPolicy.VOLATILE)
        self.status_publishers={arm:self.create_publisher(String,f'/{arm}/episode_status',qos) for arm in ('arm2','arm3')}
        for arm in self.status_publishers:
            self.create_subscription(String,f'/{arm}/episode_cmd',lambda msg,arm=arm:self.command(arm,msg),qos)
        threading.Thread(target=self.read,daemon=True).start();self.create_timer(.02,self.publish_status)
    def command(self,arm,message):
        if len(message.data)>32:
            self.status_publishers[arm].publish(String(data='?_REJECTED:COMMAND_TOO_LONG'));return
        sys.stdout.write(json.dumps({'kind':'command','arm':arm,'command':message.data})+'\n');sys.stdout.flush()
    def read(self):
        try:
            for line in sys.stdin:
                if len(line)>8192:raise ValueError('status too large')
                self.responses.put(json.loads(line),timeout=1.)
        finally:self.pipe_closed=True
    def publish_status(self):
        for _ in range(30):
            try:value=self.responses.get_nowait()
            except queue.Empty:break
            if value.get('arm') in self.status_publishers:self.status_publishers[value['arm']].publish(String(data=value['text']))
        if self.pipe_closed:rclpy.try_shutdown()


def main():
    rclpy.init(args=[]);node=EpisodeTopics()
    try:rclpy.spin(node)
    except (KeyboardInterrupt,rclpy.executors.ExternalShutdownException):pass
    finally:node.destroy_node();rclpy.try_shutdown()

if __name__=='__main__':main()
