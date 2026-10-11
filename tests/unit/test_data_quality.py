"""Data hygiene and feedback persistence regressions (mock Mongo only)."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from maillard.ai.calibration import fit_calibration, roast_quality_issues
from maillard.ai.generate import compute_heuristic_profile
from maillard.ai.history import RoastHistory, _build_summary, format_history_for_prompt, load_roast_history
from maillard.db.models import Roast
from maillard.db.repository import CoffeeRepository, CuppingRepository, RoastRepository
from maillard.db.schemas import CoffeeCreate, CoffeeUpdate, CuppingCreate, CuppingUpdate, RoastUpdate

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).parents[2] / 'scripts/cleanup_roast_history.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


def test_fixture_cleanup():
    rows = json.loads((Path(__file__).parents[1] / 'fixtures/roast_history_fixture.json').read_text())
    changes = {r['id']: cleanup.cleanup_changes(r, NOW)[1] for r in rows}
    assert changes['e2e8f9'] == {'excluded': True}
    assert changes['d7893e'] == {'excluded': True}
    assert changes['a658d1'] == {'status': 'abandoned', 'excluded': True}
    assert changes['a81180'] == {}
    assert fit_calibration(rows).excluded_roasts == sum(bool(roast_quality_issues(r)) for r in rows)


@pytest.mark.parametrize('age', [1, 24, 25])
def test_active_cleanup_age(age):
    row = {'status': 'in_progress', 'start_time': (NOW - timedelta(hours=age)).isoformat()}
    changes = cleanup.cleanup_changes(row, NOW)[1]
    assert bool(changes) == (age > 24)
    row['curve_data'] = {'events': [{'type': 'drop', 'time': 600, 'temp': 200}]}
    assert cleanup.cleanup_changes(row, NOW)[1] == {}


@pytest.mark.parametrize('field,low,high', [('altitude_m', 0, 3000), ('moisture_pct', 6, 16), ('density_g_l', 500, 900)])
@pytest.mark.parametrize('schema', [CoffeeCreate, CoffeeUpdate])
def test_coffee_bounds(field, low, high, schema):
    for value in (low, high, None):
        assert getattr(schema(name='Coffee', origin='Origin', **{field: value}), field) == value
    for value in (low - 1, high + 1, float('inf')):
        with pytest.raises(ValidationError):
            schema(name='Coffee', origin='Origin', **{field: value})


def test_moisture_small_symmetric_adjustment():
    def profile(moisture):
        return compute_heuristic_profile(RoastHistory('Coffee', 'Origin', None, None, None, coffee_moisture=moisture))
    normal, dry, wet = profile(10.5), profile(9), profile(12)
    assert dry.phases[0]['heat_percent'] == normal.phases[0]['heat_percent'] - 3
    assert wet.phases[2]['target_time_seconds'] == normal.phases[2]['target_time_seconds'] + 15
    assert profile(9.5).phases == normal.phases
    assert profile(11.5).phases == normal.phases


@pytest.mark.asyncio
async def test_history_filters_before_limit_and_feedback(mongo_db):
    coffee = await CoffeeRepository().create(CoffeeCreate(name='Coffee', origin='Origin', altitude_m=0, moisture_pct=10.5, density_g_l=700))
    good = Roast(coffee_id=coffee.id, status='completed', batch_size_grams=450, total_time_seconds=600, charge_temp=200)
    await good.insert()
    for fields in ({'excluded': True}, {'charge_temp': 22.8}, {'total_time_seconds': 344498}, {'status': 'in_progress'}):
        row = dict(coffee_id=coffee.id, status='completed', batch_size_grams=450, total_time_seconds=600, charge_temp=200)
        row.update(fields)
        await Roast(**row).insert()
    repo = CuppingRepository()
    cup = await repo.create(CuppingCreate(roast_id=good.id, notes=' Too   grassy ', next_time_notes='Extend drying'))
    cup = await repo.update(cup.id, CuppingUpdate(next_time_notes='Gentler heat'))
    history = await load_roast_history(coffee.id, limit=1)
    assert [s.roast_id for s in history.past_roasts] == [good.id]
    assert history.best_roast.roast_id == good.id
    assert history.coffee_altitude == 0
    assert history.coffee_density_g_l == 700
    prompt = format_history_for_prompt(history)
    assert 'last time: Too grassy' in prompt
    assert 'next time try: Gentler heat' in prompt
    summary = _build_summary(good, [cup])
    assert summary.next_time_notes == 'Gentler heat'
    await RoastRepository().update(good.id, RoastUpdate(excluded=True))
    assert not (await load_roast_history(coffee.id)).past_roasts
    await RoastRepository().update(good.id, RoastUpdate(excluded=False))
    assert (await load_roast_history(coffee.id)).best_roast.roast_id == good.id


@pytest.mark.asyncio
async def test_discard_patch_api(mongo_db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from maillard.core.config import MaillardConfig
    from maillard.mcp.state import ApplicationState
    from maillard.web.routes import create_router

    roast = Roast(status='completed', batch_size_grams=450, total_time_seconds=600)
    await roast.insert()
    app = FastAPI()
    app.state.maillard = ApplicationState(config=MaillardConfig())
    app.include_router(create_router())
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        for excluded in (True, False):
            result = await client.patch(f'/api/roasts/{roast.id}', json={'excluded': excluded})
            assert result.json()['excluded'] is excluded
            assert (await Roast.get(roast.id)).excluded is excluded


@pytest.mark.asyncio
@pytest.mark.parametrize('apply', [False, True])
async def test_cleanup_dry_run_and_apply(monkeypatch, apply):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    row = {'_id': 'stale', 'status': 'in_progress', 'start_time': '2020-01-01', 'drop_time': None}

    async def rows():
        yield row

    collection = MagicMock()
    collection.find.return_value = rows()
    collection.update_one = AsyncMock()
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = collection
    monkeypatch.setattr(cleanup, 'AsyncIOMotorClient', lambda **kwargs: client)
    monkeypatch.setattr(cleanup, 'get_config', lambda: SimpleNamespace(
        load_mongodb_credentials=lambda: (None, None), mongodb_host='mock', mongodb_port=27017, mongodb_name='mock'))
    await cleanup.cleanup(apply=apply)
    assert collection.update_one.await_count == int(apply)
    if apply:
        guard, update = collection.update_one.call_args.args
        assert guard['drop_time'] is None
        assert update == {'$set': {'status': 'abandoned', 'excluded': True}}
    client.close.assert_called_once()


def test_dashboard_forms_and_discard_action():
    """Exercise the existing drawer UI with a tiny DOM and API stub."""
    import shutil
    import subprocess

    if not shutil.which('node'):
        pytest.skip('Node is required to exercise dashboard JavaScript')
    static = Path(__file__).parents[2] / 'src/maillard/web/static/js'
    script = '\n'.join((static / name).read_text() for name in ('coffees.js', 'cuppings.js', 'history.js'))
    script += r'''
const assert = require('node:assert/strict');
const elements = {};
global.document = {getElementById: id => elements[id] ||= {innerHTML: '', value: '', reportValidity: () => true}};
global.App = {showAlert() {}, logEvent() {}};
global.Roast = {loadCoffees() {}, loadProfiles() {}};
let saved;
global.apiPut = async (path, body) => { saved = body; return body; };
global.apiPatch = async (path, body) => { saved = body; return body; };
global.apiGet = async () => [];
(async () => {
  Coffees.data = [{id:'c', altitude_m:0, moisture_pct:10.5, density_g_l:700}];
  Coffees.showForm('c');
  assert.match(elements['drawer-body'].innerHTML, /min="0" max="3000"/);
  assert.match(elements['drawer-body'].innerHTML, /min="6" max="16"/);
  assert.match(elements['drawer-body'].innerHTML, /min="500" max="900"/);
  for (const [id, value] of Object.entries({'cf-name':'Coffee', 'cf-origin':'Origin', 'cf-altitude':'0', 'cf-moisture':'10.5', 'cf-density-g-l':'700'})) document.getElementById(id).value=value;
  Coffees.render = () => {};
  await Coffees.save('c');
  assert.equal(saved.altitude_m, 0);
  assert.equal(saved.moisture_pct, 10.5);
  assert.equal(saved.density_g_l, 700);
  await Cuppings.showForm('r');
  assert.match(elements['cupping-list'].innerHTML, /cup-next-time/);
  document.getElementById('cup-next-time').value = 'Gentler heat';
  document.getElementById('cup-notes').value = 'Grassy';
  History.showDetail = () => {};
  await Cuppings.save('r', 'cup');
  assert.equal(saved.next_time_notes, 'Gentler heat');
  assert.equal(saved.notes, 'Grassy');
  await History.setDiscarded('r', true);
  assert.equal(saved.excluded, true);
  History.data = [{id:'r', status:'abandoned', excluded:true}];
  History._renderList(document.getElementById('drawer-body'));
  assert.match(elements['drawer-body'].innerHTML, /Discarded/);
})().catch(error => { console.error(error); process.exitCode=1; });
'''
    subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
