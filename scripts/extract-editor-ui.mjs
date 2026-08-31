import { mkdir, readFile, writeFile } from "node:fs/promises";

const source = await readFile(new URL("../app.py", import.meta.url), "utf8");
const marker = 'INDEX_HTML = r"""';
const start = source.indexOf(marker);
if (start < 0) throw new Error("INDEX_HTML marker was not found in app.py");
const bodyStart = start + marker.length;
const end = source.indexOf('\n"""\n\n\nclass MessageStudioHandler', bodyStart);
if (end < 0) throw new Error("INDEX_HTML closing marker was not found in app.py");

await mkdir(new URL("../worker-assets/", import.meta.url), { recursive: true });
const authBootstrap = `<script>
(() => {
  const stored = sessionStorage.getItem("messageCenterAdminToken");
  const token = stored || window.prompt("Enter the Message Center administrator token");
  if (!token) return;
  sessionStorage.setItem("messageCenterAdminToken", token);
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const request = new Request(input, init);
    if (!new URL(request.url, window.location.origin).pathname.startsWith("/api/")) return originalFetch(request);
    const headers = new Headers(request.headers);
    headers.set("Authorization", "Bearer " + token);
    return originalFetch(new Request(request, { headers }));
  };
})();
</script>`;
const html = source.slice(bodyStart, end).replace("</head>", `${authBootstrap}</head>`);
await writeFile(new URL("../worker-assets/index.html", import.meta.url), html, "utf8");
