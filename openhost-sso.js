/* Bootstrap the authenticated OpenHost owner into Stoat's native auth store. */
(async () => {
  const launchStoat = () => import("/assets/index-NqnvUoWC.js");
  if (sessionStorage.getItem("openhost-stoat-sso") === "ready") {
    await launchStoat();
    return;
  }

  const response = await fetch("/openhost/owner-session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 403) {
    await launchStoat();
    return;
  }
  if (!response.ok) throw new Error(`OpenHost owner SSO failed (${response.status})`);

  const session = await response.json();
  // Omitting a version opens an existing LocalForage database at its current
  // version and still creates version 1 on a clean browser profile.
  const request = indexedDB.open("localforage");
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
      launchStoat().catch((error) => console.error("[openhost-sso]", error));
    };
    transaction.onerror = () => {
      throw transaction.error;
    };
  };
})().catch((error) => console.error("[openhost-sso]", error));
