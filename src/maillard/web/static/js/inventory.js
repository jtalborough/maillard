// Green lots and presale drops in the existing dashboard drawer.
const Inventory = {
  esc(value) { const el = document.createElement('span'); el.textContent = value ?? ''; return el.innerHTML; },
  grams(value) { return `${Number(value).toFixed(1)} g`; },
  async render(container) {
    this.container = container;
    const [summary, drops] = await Promise.all([apiGet('/api/inventory/summary'), apiGet('/api/drops')]);
    if (!summary || !drops) { container.textContent = 'Inventory unavailable'; return; }
    const options = summary.map(c => `<option value="${this.esc(c.coffee_id)}">${this.esc(c.coffee_name)}</option>`).join('');
    container.innerHTML = `<div class="space-y-4 text-sm">
      <p class="text-slate-400">Green is counted from lots. No lots means unknown until the physical count is imported. Roasting stays supervised.</p>
      <button onclick="Inventory.render(Inventory.container)" class="bg-slate-700 px-2 py-1 rounded">Refresh</button>
      ${summary.map(c => `<section class="bg-slate-800 rounded p-3"><h3 class="font-semibold">${this.esc(c.coffee_name)}</h3>
        <p>Remaining ${this.grams(c.remaining)} · Reserved ${this.grams(c.reserved)} · Free ${this.grams(c.free)} ${c.known ? '' : '(unknown)'}</p>
        ${c.lots.map(l => `<p class="text-xs text-slate-400 mt-1">${this.esc(l.received_at.slice(0,10))} ${this.esc(l.supplier || '')}: ${this.grams(l.grams_remaining)} remaining, ${this.grams(l.reserved)} reserved <button class="underline" onclick="Inventory.adjust('${this.esc(l.id)}')">Adjust</button></p>`).join('')}</section>`).join('')}
      <form id="inventory-receipt" class="space-y-2 border-t border-slate-700 pt-3">
        <h3 class="font-semibold">Receive green</h3>
        <select name="coffee_id" class="bg-slate-800 w-full rounded p-1">${options}</select>
        <input name="grams_in" type="number" min="0.1" step="0.1" placeholder="Grams received" required class="bg-slate-800 w-full rounded p-1">
        <input name="supplier" placeholder="Supplier" class="bg-slate-800 w-full rounded p-1">
        <button class="bg-amber-700 px-2 py-1 rounded">Receive lot</button>
      </form>
      <form id="inventory-drop" class="space-y-2 border-t border-slate-700 pt-3">
        <h3 class="font-semibold">Open presale drop</h3>
        <select name="coffee_id" class="bg-slate-800 w-full rounded p-1">${options}</select>
        <input name="capacity_bags" type="number" min="1" step="1" placeholder="Capacity in bags" required class="bg-slate-800 w-full rounded p-1">
        <input name="bag_size_grams" type="number" min="1" value="340" required class="bg-slate-800 w-full rounded p-1" aria-label="Bag size grams">
        <input name="expected_yield" type="number" min="0.01" max="1" step="0.01" value="0.85" required class="bg-slate-800 w-full rounded p-1" aria-label="Expected yield">
        <button class="bg-amber-700 px-2 py-1 rounded">Reserve green</button>
      </form>
      <h3 class="font-semibold">Drops</h3>
      ${drops.map(d => `<section class="bg-slate-800 rounded p-3"><h4>Drop ${d.drop_number ?? 'pending sync'} · ${this.esc(d.status)} · ${this.esc(summary.find(c => c.coffee_id === d.coffee_id)?.coffee_name)}</h4>
        <p>${d.capacity_bags} capacity · ${d.bags_sold} sold · ${d.bags_fulfilled} fulfilled</p>
        <p>${this.grams(d.green_grams_per_bag)} green / bag · ${this.grams(d.green_reserved_grams)} reserved</p>
        <p class="text-xs break-all">Drop id: ${this.esc(d.id)}</p>
        <div class="flex gap-3 mt-2">${['sync','close','cancel'].map(a => `<button class="underline" onclick="Inventory.dropAction('${this.esc(d.id)}','${a}')">${a}</button>`).join('')}
        ${['open','closed'].includes(d.status) ? `<button class="underline" onclick="Inventory.selectDrop('${this.esc(d.id)}','${this.esc(d.coffee_id)}')">Use for next roast</button>` : ''}</div></section>`).join('')}
      <p id="inventory-message" class="text-red-400"></p></div>`;
    document.getElementById('inventory-receipt').onsubmit = async e => {
      e.preventDefault(); const data = Object.fromEntries(new FormData(e.target)); data.grams_in = Number(data.grams_in);
      await this.result(await apiPost('/api/inventory/lots', data));
    };
    document.getElementById('inventory-drop').onsubmit = async e => {
      e.preventDefault(); const data = Object.fromEntries(new FormData(e.target));
      for (const key of ['capacity_bags','bag_size_grams','expected_yield']) data[key] = Number(data[key]);
      await this.result(await apiPost('/api/drops', data));
    };
  },
  async result(data) {
    if (!data || data.status === 'error') document.getElementById('inventory-message').textContent = data?.error || 'Request failed';
    else await this.render(this.container);
  },
  async adjust(id) {
    const grams = prompt('Signed adjustment in grams (example: -100)'); if (grams === null || grams.trim() === '') return;
    const note = prompt('Reason for adjustment'); if (!note) return;
    await this.result(await apiPost(`/api/inventory/lots/${id}/adjust`, {grams: Number(grams), note}));
  },
  async dropAction(id, action) { await this.result(await apiPost(`/api/drops/${id}/${action}`)); },
  clearDrop() {
    App.nextDropId = null;
    document.getElementById('charge-drop-info').textContent = 'No presale drop selected';
  },
  selectDrop(id, coffeeId) {
    App.nextDropId = id;
    document.getElementById('charge-drop-info').textContent = `Presale drop: ${id}`;
    const select = document.getElementById('coffee-select');
    if (select) { select.value = coffeeId; Roast.onCoffeeChange(); }
    App.logEvent(`Next roast linked to drop ${id}; choose its coffee before charge`, 'text-amber-300');
    App.closeDrawer();
  },
};
