"""Operator-facing names only; wire commands and stored result codes stay intact."""
STATES={
    'CONNECTING':'연결 확인 중','ONLINE':'연결됨','COMM_LOST':'통신 끊김',
    'UNKNOWN':'결과 미확인','PENDING':'처리 결과 대기','STORAGE_PENDING':'결과 기록 저장 대기',
    'NOT_FOUND':'미접수 확인','CLEARED':'미확정 기록 정리됨','IDLE':'대기',
    'RUNNING':'실행 중','PAUSED':'작업 보류','ERROR':'실패','BUILD_DONE':'조립 완료 · 적재 결과 대기',
}
ARMS={'arm2':'팔2','arm3':'팔3'}
def state_name(value):return STATES.get(value,value)
def arm_name(value):return ARMS.get(value,value)
