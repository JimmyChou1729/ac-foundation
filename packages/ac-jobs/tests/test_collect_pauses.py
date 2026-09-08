from ac_jobs import RunRepository, RunSpec, RunContext, WorkUnit, FailureMode, Paused, Awaiting, ResumeReason


def test_local_pause_finishes_neighbors_and_replay_only_runs_paused_unit(tmp_path):
    repo = RunRepository(tmp_path)
    ctx = RunContext(repo, repo.create(RunSpec('local', 'test', {})), resume_input=None)
    calls = []
    def worker(unit):
        calls.append(unit.unit_id)
        if unit.unit_id == 'a':
            return Paused(Awaiting(ResumeReason.SUPERVISION_REQUIRED, 'local', False))
        return {'done': unit.unit_id}
    units = tuple(WorkUnit(i, {}) for i in ['a','b','c'])
    result = ctx.run_group('g', units, worker, max_workers=1,
        failure_mode=FailureMode.COLLECT, continue_after_pause=lambda p:True)
    assert isinstance(result, Paused)
    assert calls == ['a','b','c']
    calls.clear()
    ctx.run_group('g', units, worker, max_workers=1,
        failure_mode=FailureMode.COLLECT, continue_after_pause=lambda p:True)
    assert calls == ['a']


def test_shared_pause_stops_admission_and_takes_precedence(tmp_path):
    repo = RunRepository(tmp_path)
    ctx = RunContext(repo, repo.create(RunSpec('shared', 'test', {})), resume_input=None)
    calls = []
    def worker(unit):
        calls.append(unit.unit_id)
        return Paused(Awaiting(ResumeReason.SUPERVISION_REQUIRED if unit.unit_id=='a'
            else ResumeReason.EXTERNAL_CONDITION, unit.unit_id, False))
    result = ctx.run_group('g', tuple(WorkUnit(i,{}) for i in ['a','b','c']), worker,
        max_workers=1, failure_mode=FailureMode.COLLECT,
        continue_after_pause=lambda p:p.awaiting.resume_key=='a')
    assert result.awaiting.resume_key == 'b'
    assert calls == ['a','b']
