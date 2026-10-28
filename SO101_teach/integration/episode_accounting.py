"""Read-only accounting of observations; no device access or control decisions."""
import math


def union_seconds(intervals):
    """Measure the union, not the sum, of overlapping/nested intervals."""
    valid=sorted((a,b) for a,b in intervals if type(a) in (int,float) and type(b) in (int,float)
                 and math.isfinite(a) and math.isfinite(b) and b>=a)
    total=0.;start=end=None
    for a,b in valid:
        if end is None:start,end=a,b
        elif a<=end:end=max(end,b)
        else:total+=end-start;start,end=a,b
    return total+(end-start if end is not None else 0.)


def summarize(analysis,timing=None):
    """Names explicitly distinguish elapsed wall time from inclusive observations."""
    a=analysis;end=a.get('finished_elapsed_s');complete=bool(a.get('record_complete'))
    end=end if complete else a.get('last_event_elapsed_s',0.)
    def clip(rows):
        return [(max(0.,x),min(end,y)) for x,y in rows
                if type(x) in (int,float) and type(y) in (int,float) and y>=0 and x<=end]
    motion=clip([(x.get('started_elapsed_s'),x.get('finished_elapsed_s')) for x in a.get('motion_calls',[])])
    linear=clip([(x.get('started_elapsed_s'),x.get('ended_elapsed_s')) for x in a.get('linear_intervals',[])])
    pauses=clip([(x.get('started_elapsed_s'),x.get('ended_elapsed_s')) for x in a.get('pause_intervals',[])])
    profiles=a.get('preparation_profiles',{});prep=[];dropped=0
    for p in profiles.values():
        # Profiles and job clocks originate in the same executor process.
        origin=p.get('job_origin_offset_s')
        if origin is None:continue
        prep.extend((x['started_seconds']+origin,x['finished_seconds']+origin) for x in p.get('intervals',[]))
        dropped+=p.get('dropped_intervals',0)
    prep=clip(prep)
    motion_s=union_seconds(motion);linear_s=union_seconds(linear)
    observed=union_seconds([*motion,*linear,*prep,*pauses])
    expected=a.get('expected_inspections',{});checks={}
    for arm,rows in expected.items():
        ids={r['step_id'] for r in rows};records=a.get('inspection_by_arm',{}).get(arm,{}).get('records',[])
        passed={r.get('step_id') for r in records if r.get('result')=='PASS'}
        failed=any(r.get('result')=='FAIL' for r in records)
        checks[arm]={'expected_unique':len(ids),'passed_expected_unique':len(ids&passed),
                     'missing_step_ids':sorted(ids-passed),'has_failure':failed,
                     'complete':bool(ids) and ids<=passed and not failed}
    expected_steps={(m['motion_id'],i) for m in a.get('motion_calls',[]) if m.get('requests') or m.get('send_attempts')
                    for i in range(1,len(m.get('targets_ticks',[]))+1)}
    unacknowledged=sum(r.get('request_id') is None for m in a.get('motion_calls',[]) for r in m.get('send_attempts',[]))
    legacy_uncertain=any('send_attempts' not in m and not m.get('requests') and m.get('targets_ticks')
                         for m in a.get('motion_calls',[]))
    arrivals=a.get('step_arrivals',[])
    observed_steps={(r['motion_id'],r['original_step_index']) for r in arrivals}
    arrivals_available=a.get('schema_version',1)>=2
    inspection_complete=bool(checks) and all(x['complete'] for x in checks.values())
    return {'schema_version':1,'job_id':a['job_id'],'count_unit':'job_id',
        'outcome':a.get('outcome'),'record_complete':complete,'category':a.get('category'),
        'successful_production':complete and a.get('category')=='production' and a.get('outcome')=='succeeded',
        'uninterrupted_successful_production':complete and a.get('category')=='production'
            and a.get('outcome')=='succeeded' and a.get('completed_without_intervention') is True,
        'inspection':checks,'expected_inspections_recorded_pass':inspection_complete,
        'time':{'clock':'executor_monotonic','terminal_elapsed_seconds':end if complete else None,
            'phase_summary':timing,'controller_call_union_seconds':motion_s,
            'linear_moving_observation_union_seconds':linear_s,
            'controller_linear_overlap_seconds':max(0.,motion_s+linear_s-union_seconds([*motion,*linear])),
            'pause_observation_union_seconds':union_seconds(pauses),
            'preparation_measured_span_union_seconds':union_seconds(prep) if profiles else None,
            'all_observed_span_union_seconds':observed,
            'elapsed_not_covered_by_spans_seconds':max(0.,end-observed),
            'complete_intervals_only':True,'dropped_preparation_intervals':dropped,
            'note':'Nested/concurrent spans overlap. Uncovered time is not a measured idle time; controller calls are not pure motor-moving time.'},
        'linear':{'requests':len(a.get('linear_requests',[])),
            'already_at_target':sum(r.get('outcome')=='already_at_target' for r in a.get('linear_requests',[])),
            'moved':sum(r.get('outcome')=='moved' for r in a.get('linear_requests',[])),
            'unresolved':sum(r.get('outcome') in ('checking','moving') for r in a.get('linear_requests',[])),
            'skip_logging_available':a.get('schema_version',1)>=2,'position_measured':False},
        'arrival_coverage':{'logging_available':arrivals_available,
            'unacknowledged_send_attempts':unacknowledged,
            'coverage_state':'unavailable' if not arrivals_available else 'indeterminate' if unacknowledged or legacy_uncertain
                else 'incomplete' if expected_steps-observed_steps else 'complete','expected_original_steps':len(expected_steps),
            'matched_original_steps':len(expected_steps&observed_steps) if arrivals_available else None,
            'missing_original_steps':[list(x) for x in sorted(expected_steps-observed_steps)] if arrivals_available else None,
            'transport_gaps':len(a.get('arrival_transport_gaps',[])),
            'observations':len(arrivals),'settled':sum(r.get('arrival_type')=='settled' for r in arrivals),
            'pass_through':sum(r.get('arrival_type')=='pass_through' for r in arrivals),
            'residual_accepted':sum(r.get('arrival_type')=='residual_accepted' for r in arrivals),
            'gripper_target_adjusted':sum(r.get('gripper_target_adjusted',False) for r in arrivals)},
        'measurement_limits':['Successful-only samples cannot estimate yield or classification accuracy.',
            'Arrival residuals are encoder tracking errors, not externally measured TCP/product accuracy.',
            'Compare within operation, configuration hashes and runtime version.']}


def select_jobs(records):
    """Choose one authoritative snapshot per job; legacy notices are not jobs."""
    jobs={};ignored=0;conflicts=[]
    for record in records:
        a=record.get('analysis') or {}
        if record.get('record_kind')!='execution' or not a.get('job_id'):
            ignored+=1;continue
        key=a['job_id'];old=jobs.get(key)
        rank=lambda r:(bool(r['analysis'].get('record_complete')),
            r['analysis'].get('event_count',0),r['analysis'].get('last_event_elapsed_s',-1))
        if old:
            oa=old['analysis']
            if a.get('record_complete') and oa.get('record_complete') and a.get('outcome')!=oa.get('outcome'):
                conflicts.append(key)
        if old is None or rank(record)>rank(old):jobs[key]=record
    return jobs,{'ignored_non_execution_records':ignored,'conflicting_terminal_jobs':sorted(set(conflicts))}
