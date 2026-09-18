(() => {
  const side = document.querySelector(".side");
  if (!side) return;
  const brand = side.querySelector(".brand");
  const sub = side.querySelector(".sub");
  if (brand) brand.textContent = "Message Center";
  if (sub) sub.textContent = "Select a group first, then optionally narrow it by category.";
  const filters = document.createElement("section");
  filters.className = "studio-filters";
  filters.innerHTML = `
    <button id="studio-new-message" type="button">New Message</button>
    <label>Group<select id="studio-group"></select></label>
    <div class="studio-buttons"><button class="ghost" type="button">Edit group</button><button class="remove" type="button">Remove group</button></div>
    <label>Category<select id="studio-category-select"></select></label>
    <div class="studio-buttons"><button class="ghost" type="button" disabled>Edit category</button><button class="remove muted" type="button" disabled>Remove category</button></div>
    `;
  const importBox = side.querySelector(".import");
  importBox?.after(filters);
  const label = value => value.replace(/^postoochat_/, "Postoochat ").replace(/_/g, " ").replace(/\b\w/g, letter => letter.toUpperCase());
  const groupSelect = filters.querySelector("#studio-group");
  const categorySelect = filters.querySelector("#studio-category-select");
  const newMessageButton = filters.querySelector("#studio-new-message");
  const request = async (path, init = {}) => { const token = sessionStorage.getItem("message_center_admin_token"); const response = await fetch(path, { headers: { "content-type": "application/json", ...(token ? { authorization: `Bearer ${token}` } : {}) }, ...init }); const payload = await response.json(); if (response.status === 401) { const supplied = window.prompt("Enter the Message Center admin token to continue."); if (supplied) { sessionStorage.setItem("message_center_admin_token", supplied.trim()); return request(path, init); } } if (!response.ok) throw new Error(payload.error || "Request failed"); return payload; };
  const applyFilter = () => window.dispatchEvent(new CustomEvent("message-studio-filter", { detail: { group: groupSelect.value, category: categorySelect.value } }));
  const populate = async (groupToSelect, categoryToSelect = "") => {
    const catalog = await request("/api/catalog");
    const selectedGroup = (groupToSelect ?? groupSelect.value) || Object.keys(catalog.taxonomy)[0] || "";
    groupSelect.innerHTML = `<option value="">Select group</option>${Object.keys(catalog.taxonomy).sort().map(group => `<option value="${group}">${label(group)}</option>`).join("")}<option value="__new__">Add a new group…</option>`;
    groupSelect.value = selectedGroup;
    const categories = catalog.taxonomy[selectedGroup] || [];
    categorySelect.innerHTML = `<option value="">All categories</option>${categories.map(category => `<option value="${category}">${category}</option>`).join("")}<option value="__new__">Add a new category…</option>`;
    categorySelect.value = categoryToSelect;
    applyFilter();
  };
  groupSelect.onchange = async () => {
    if (groupSelect.value !== "__new__") return populate(groupSelect.value);
    const value = window.prompt("New group name");
    if (!value?.trim()) return populate();
    try { await request("/api/taxonomy", { method: "POST", body: JSON.stringify({ action: "create_group", group: value }) }); const created = value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, ""); await populate(created); }
    catch (error) { window.alert(`Could not add group: ${error.message}`); await populate(); }
  };
  categorySelect.onchange = async () => {
    if (categorySelect.value !== "__new__") return applyFilter();
    const group = groupSelect.value;
    if (!group) return window.alert("Select a group before adding a category.");
    const value = window.prompt("New category name");
    if (!value?.trim()) return populate();
    try { await request("/api/taxonomy", { method: "POST", body: JSON.stringify({ action: "create_category", group, category: value }) }); await populate(group, value.trim()); }
    catch (error) { window.alert(`Could not add category: ${error.message}`); await populate(); }
  };
  newMessageButton.onclick = () => {
    const group = groupSelect.value;
    const category = categorySelect.value;
    if (!group) return window.alert("Select a group before adding a message.");
    if (!category) return window.alert("Select a category before adding a message.");
    window.dispatchEvent(new CustomEvent("message-studio-create", { detail: { group, category } }));
  };
  populate().catch(() => {});
})();
