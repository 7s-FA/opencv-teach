# Source this file. Sourcing defines commands only; it never moves a robot.
source /opt/ros/jazzy/setup.bash
export ROS_DOMAIN_ID=40

episode() {
    if [[ $# -ne 2 || ! $1 =~ ^arm[23]$ || ! $2 =~ ^(A|B|STOP)$ ]]; then
        echo 'Usage: episode arm2|arm3 A|B|STOP' >&2
        return 2
    fi
    /usr/bin/python3 - "$1" "$2" <<'PYTHON'
import os,sys,time
import rclpy
from rclpy.duration import Duration
from std_msgs.msg import String

def main():
    arm,command=sys.argv[1:]
    rclpy.init(args=[])
    node=rclpy.create_node('episode_command_'+str(os.getpid()))
    replies=[]
    publisher=node.create_publisher(String,f'/{arm}/episode_cmd',10)
    subscription=node.create_subscription(String,f'/{arm}/episode_status',lambda message:replies.append(message.data),10)
    try:
        deadline=time.monotonic()+float(os.environ.get('SO101_EPISODE_WAIT_SECONDS','8'))
        while rclpy.ok() and time.monotonic()<deadline:
            rclpy.spin_once(node,timeout_sec=.1)
            if publisher.get_subscription_count()>0 and node.count_publishers(f'/{arm}/episode_status')>0:break
        else:
            print('명령을 보내지 않았습니다: PC 통합 수신 앱이 없거나 ROS 연결이 되지 않았습니다.',file=sys.stderr)
            print('PC에서 /path/to/Final_Arm/SO101_teach/run_integration.sh 를 실행하세요.',file=sys.stderr)
            return 2
        replies.clear();publisher.publish(String(data=command))
        publisher.wait_for_all_acked(Duration(seconds=2))
        deadline=time.monotonic()+3
        while rclpy.ok() and time.monotonic()<deadline:
            rclpy.spin_once(node,timeout_sec=.1)
            for reply in replies:
                matches=reply.startswith((command+'_ACCEPTED',command+'_REJECTED'))
                if command=='STOP':matches=reply.startswith(('A_STOPPED','B_STOPPED','STOP_REJECTED'))
                if matches:
                    print(reply)
                    return 1 if '_REJECTED:' in reply else 0
        print(f'명령은 발행했지만 접수 응답을 확인하지 못했습니다. 다시 보내기 전에 {arm}_status로 확인하세요.',file=sys.stderr)
        return 3
    finally:
        node.destroy_node();rclpy.try_shutdown()

sys.exit(main())
PYTHON
}
arm2_a() { episode arm2 A; }
arm2_b() { episode arm2 B; }
arm3_a() { episode arm3 A; }
arm3_b() { episode arm3 B; }
arm2_stop() { episode arm2 STOP; }
arm3_stop() { episode arm3 STOP; }
arm2_status() { ros2 topic echo /arm2/episode_status std_msgs/msg/String; }
arm3_status() { ros2 topic echo /arm3/episode_status std_msgs/msg/String; }
