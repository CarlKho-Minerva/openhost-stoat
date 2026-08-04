/* Bootstrap the authenticated OpenHost owner into Stoat's native auth store. */
(async () => {
  if (sessionStorage.getItem("openhost-stoat-sso") === "ready") return;

  const response = await fetch("/openhost/owner-session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 403) return;
  if (!response.ok) throw new Error(`OpenHost owner SSO failed (${response.status})`);

  const session = await response.json();
  const request = indexedDB.open("localforage", 1);
  request.onupgradeneeded = () => {
    if (!request.result.objectStoreNames.contains("keyvaluepairs")) {
      request.result.createObjectStore("keyvaluepairs");
    }
  };
  request.onerror = () => {
    throw request.error;
  };
  request.onsuccess = () => {
    const transaction = request.result.transaction("keyvaluepairs", "readwrite");
    transaction.objectStore("keyvaluepairs").put({ session }, "auth");
    transaction.oncomplete = () => {
      sessionStorage.setItem("openhost-stoat-sso", "ready");
      location.reload();
    };
    transaction.onerror = () => {
      throw transaction.error;
    };
  };
})().catch((error) => console.error("[openhost-sso]", error));
