const rows = [...document.querySelectorAll(".bill")];
function apply() {
  const q = document.getElementById("q").value.toLowerCase().replace(/\./g, "");
  const s = document.getElementById("s").value;
  let n = 0;
  for (const r of rows) {
    const ok = (!s || r.dataset.label === s) && (!q || r.dataset.search.includes(q));
    r.hidden = !ok;
    if (ok) n++;
  }
  document.getElementById("shown").textContent = n + " shown";
}
document.getElementById("q").oninput = apply;
document.getElementById("s").onchange = apply;
apply();
