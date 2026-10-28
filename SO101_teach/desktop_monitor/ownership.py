"""Release idle teaching leases before observing externally controlled work."""


def targets(app):
    manager=getattr(app,'workspace_manager',None)
    return list(manager.apps.values()) if manager else [app]


def active(app):
    from so101_teach.pi_execution import active_execution
    session=app.session
    return bool(active_execution(app) or app.pending_execution or app.camera_task or app.remote_plan_job
        or getattr(getattr(app,'inspection_run',None),'busy',False)
        or getattr(app.settings.worker,'running',False)
        or app.settings.job
        or getattr(app.leader_session,'running',False)
        or session and session.running and (session.state not in ('READ_ONLY','HOLD')
            or session.program_active.is_set() or session.command_pending.is_set()))


def prepare(app):
    """Called on the Tk thread; network cleanup runs in the existing pool."""
    panel=app.camera_work_monitor
    def retry():
        if not panel.closed and app.page=='camera' and app.work_monitor_selected():
            if panel.observer and not panel.observer.stop.is_set():prepare(app)
            else:panel.activate()
    def schedule(delay,callback=retry):
        if panel.handoff_job:panel.after_cancel(panel.handoff_job)
        def invoke():
            panel.handoff_job=None;callback()
        panel.handoff_job=panel.after(delay,invoke)
    pending=getattr(app,'_observation_handoff',None)
    if pending:
        if not pending.done():return False
        app._observation_handoff=None
        try:pending.result()
        except Exception as exc:
            panel.connection.set('티칭 연결 정리 실패 · '+str(exc));return False
        app.notice('티칭 제어 연결 해제 · 조회 전용')
        return True
    windows=targets(app)
    if any(active(a) for a in windows):
        app.notice('티칭 동작·준비 중 · 제어 연결 유지',False)
        schedule(250)
        return True  # Observation itself is safe; never detach an active owner.
    for a in windows:
        a.work_observation_mode=True;a._work_monitor_passive=True
        a.startup_devices_pending=False
        if getattr(a,'startup_job',None):a.root.after_cancel(a.startup_job);a.startup_job=None
        pi=a.settings.pi_panel;pi.auto_enabled=False;pi.recovery_pending=False;pi.restore_devices=False
    if any(a.settings.pi_panel.job for a in windows):
        panel.connection.set('티칭 연결 준비 완료 대기 · 조회 전환 중')
        schedule(100)
        return False
    links=[];cameras=[]
    for a in windows:
        if not a.remote:continue
        link=a.remote
        if a.camera:
            # Closing this PC receiver must not issue camera_stop against a new owner.
            a.camera.observer=True;a.camera.close();cameras.append(a.camera)
        a.camera_yolo.stop('실시간 작업 조회')
        a.live_adjust.stop(halt=False)
        links.append(link)
        a.remote=None;a.session=None;a.camera=None;a.leader_session=None;a.latest=None
        a.connection.set('조회 전용 · 제어 연결 해제');a.device_target.set('장치: Pi · 조회 전용')
    if not links:return True
    def release():
        for camera in cameras:camera.join(2)
        for link in links:
            # detach preserves idle follower torque; disconnect would release it.
            link.close(disconnect=False)
            for thread in (link.state_thread,link.leader_thread):
                if thread:thread.join(2)
            link.listeners.clear()
    app._observation_handoff=app.settings.pool.submit(release)
    panel.connection.set('티칭 제어 연결 해제 중 · 조회 전환 중')
    def finished():
        if panel.closed:return
        if not app._observation_handoff or app._observation_handoff.done():retry()
        else:schedule(80,finished)
    schedule(80,finished)
    return False
