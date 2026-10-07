/**
 * NearBeam - Web Application Client Script
 * Handles Safe List browsing, drag & drop auto-save uploads,
 * real-time SSE synchronization, clipboard sync, and media previews.
 */

// State
let currentFolderId = "";
let currentSubpath = "";
let safeItemsCache = [];
let cachedSafeFolders = [];
let serverInfo = null;
let meshNodes = [];
let selectedTargetNodeId = "self";

// DOM Elements
const hostNameDisplay = document.getElementById("hostNameDisplay");
const meshClusterPill = document.getElementById("meshClusterPill");
const meshClusterText = document.getElementById("meshClusterText");
const selectSafeFolder = document.getElementById("selectSafeFolder");
const safeFilesContainer = document.getElementById("safeFilesContainer");
const breadcrumbBar = document.getElementById("breadcrumbBar");
const btnSafeBack = document.getElementById("btnSafeBack");
const safeBackBtnLabel = document.getElementById("safeBackBtnLabel");
const safeSearchInput = document.getElementById("safeSearchInput");
const btnDownloadFolderZip = document.getElementById("btnDownloadFolderZip");
const btnRefreshSafe = document.getElementById("btnRefreshSafe");
const badgeSafeCount = document.getElementById("badgeSafeCount");

// Upload DOM Elements
const dropzone = document.getElementById("dropzone");
const meshDestinationBar = document.getElementById("meshDestinationBar");
const destinationPills = document.getElementById("destinationPills");
const fileInput = document.getElementById("fileInput");
const folderInput = document.getElementById("folderInput");
const btnSelectFiles = document.getElementById("btnSelectFiles");
const btnSelectFolder = document.getElementById("btnSelectFolder");
const uploadProgressCard = document.getElementById("uploadProgressCard");
const progressFileName = document.getElementById("progressFileName");
const progressSpeed = document.getElementById("progressSpeed");
const progressPct = document.getElementById("progressPct");
const progressBarFill = document.getElementById("progressBarFill");
const progressTransferred = document.getElementById("progressTransferred");
const progressEta = document.getElementById("progressEta");
const sentList = document.getElementById("sentList");

// Received DOM Elements
const receivedFilesContainer = document.getElementById("receivedFilesContainer");
const receivedPathDesc = document.getElementById("receivedPathDesc");
const btnRefreshReceived = document.getElementById("btnRefreshReceived");
const incomingTransferCard = document.getElementById("incomingTransferCard");
const incomingTransfersList = document.getElementById("incomingTransfersList");

// Upload sessions started by THIS device, so we don't also show them as
// "incoming" (this device already shows its own bar on the Send tab).
const myUploadSessions = new Set();

// Clipboard DOM Elements
const clipboardTextarea = document.getElementById("clipboardTextarea");
const btnSendClipboard = document.getElementById("btnSendClipboard");
const btnSendClipboardLabel = document.getElementById("btnSendClipboardLabel");
const btnCopyClipboard = document.getElementById("btnCopyClipboard");
const clipboardUpdatedTime = document.getElementById("clipboardUpdatedTime");
const chkKeepSyncingClipboard = document.getElementById("chkKeepSyncingClipboard");
const syncLiveStatus = document.getElementById("syncLiveStatus");

// Modals DOM Elements
const previewModal = document.getElementById("previewModal");
const previewModalTitle = document.getElementById("previewModalTitle");
const previewModalBody = document.getElementById("previewModalBody");
const btnClosePreview = document.getElementById("btnClosePreview");
const btnModalDownload = document.getElementById("btnModalDownload");

const qrModal = document.getElementById("qrModal");
const btnQrModal = document.getElementById("btnQrModal");
const btnCloseQr = document.getElementById("btnCloseQr");
const qrUrlText = document.getElementById("qrUrlText");

// ZIP Download Progress Modal DOM Elements
const zipProgressModal = document.getElementById("zipProgressModal");
const btnCloseZipModal = document.getElementById("btnCloseZipModal");
const btnCancelZip = document.getElementById("btnCancelZip");
const btnCancelZipLabel = document.getElementById("btnCancelZipLabel");
const zipModalFolderName = document.getElementById("zipModalFolderName");
const zipModalFolderText = document.getElementById("zipModalFolderText");
const zipProgressCircle = document.getElementById("zipProgressCircle");
const zipPercentLabel = document.getElementById("zipPercentLabel");
const zipStatusBadge = document.getElementById("zipStatusBadge");
const zipCurrentFile = document.getElementById("zipCurrentFile");
const zipFilesCount = document.getElementById("zipFilesCount");
const zipBytesCount = document.getElementById("zipBytesCount");

let activeZipJobId = null;
let zipProgressTimer = null;
const ZIP_RING_CIRCUMFERENCE = 326.7;

// Theme
const btnThemeToggle = document.getElementById("btnThemeToggle");

// SVG Icons mapping
const categoryIcons = {
  folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>`,
  image: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>`,
  video: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>`,
  audio: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>`,
  document: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>`,
  archive: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="21 8 21 21 3 21 3 8"></polyline><rect x="1" y="3" width="22" height="5"></rect><line x1="10" y1="12" x2="14" y2="12"></line></svg>`,
  code: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline></svg>`,
  file: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>`,
};

// --- Initialization ---

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  setupTabs();
  fetchServerInfo();
  fetchSafeFolders();
  fetchClipboard();
  fetchMeshNodes();
  setupClipboardSync();
  setupUploadEvents();
  setupSSE();
  setupModalEvents();
  setupFaqEvents();
});

// --- Theme Toggle ---

function initTheme() {
  const saved = localStorage.getItem("nearbeam_theme") || localStorage.getItem("landrop_theme");
  if (saved === "light") {
    document.body.classList.remove("theme-dark");
    document.body.classList.add("theme-light");
  }
  btnThemeToggle.addEventListener("click", () => {
    if (document.body.classList.contains("theme-light")) {
      document.body.classList.remove("theme-light");
      document.body.classList.add("theme-dark");
      localStorage.setItem("nearbeam_theme", "dark");
    } else {
      document.body.classList.remove("theme-dark");
      document.body.classList.add("theme-light");
      localStorage.setItem("nearbeam_theme", "light");
    }
  });
}

// --- Tabs Management ---

function setupTabs() {
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabPanes = document.querySelectorAll(".tab-pane");

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-tab");
      tabBtns.forEach((b) => b.classList.remove("active"));
      tabPanes.forEach((p) => p.classList.remove("active"));

      btn.classList.add("active");
      const targetPane = document.getElementById(targetId);
      if (targetPane) targetPane.classList.add("active");

      if (targetId === "tab-received") {
        fetchReceivedFiles();
      } else if (targetId === "tab-clipboard") {
        fetchClipboard();
      }
    });
  });
}

// --- Toast Notifications ---

function showToast(message, type = "info") {
  const container = document.getElementById("toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerText = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// --- Server Info ---

async function fetchServerInfo() {
  try {
    const res = await fetch("/api/info");
    if (!res.ok) return;
    serverInfo = await res.json();

    const hostLabel = serverInfo.nearbeam_url || serverInfo.landrop_url || `${serverInfo.hostname}.local`;
    hostNameDisplay.innerText = hostLabel;
    qrUrlText.innerText = serverInfo.nearbeam_url || serverInfo.landrop_url || serverInfo.primary_url;

    const faqDirectIp = document.getElementById("faqDirectIpDisplay");
    if (faqDirectIp && serverInfo.primary_url) {
      faqDirectIp.innerText = serverInfo.primary_url;
    }
  } catch (e) {
    hostNameDisplay.innerText = "Offline / Connection Error";
  }
}

// Click on host pill copies the permanent URL
hostPill.addEventListener("click", async () => {
  const url = serverInfo?.nearbeam_url || serverInfo?.landrop_url || "http://nearbeam.local:5000";
  try {
    await navigator.clipboard.writeText(url);
    showToast("✓ Copied " + url + " to clipboard!", "success");
  } catch (e) {
    showToast(url, "info");
  }
});

// --- Safe List Folder Explorer ---

function getParentSubpath(subpath) {
  if (!subpath) return "";
  const normalized = subpath.replace(/\\/g, "/").replace(/\/+$/, "");
  const parts = normalized.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

function showAllSafeFolders(pushHistory = true) {
  currentFolderId = "";
  currentSubpath = "";
  if (selectSafeFolder) selectSafeFolder.value = "";
  if (btnDownloadFolderZip) btnDownloadFolderZip.style.display = "none";
  if (btnSafeBack) btnSafeBack.style.display = "none";

  breadcrumbBar.innerHTML = `<span class="breadcrumb-item active"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;vertical-align:-2px;margin-right:4px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>Shared Safe Folders</span>`;

  if (pushHistory) {
    window.history.pushState({ view: "safelist_root" }, "", window.location.pathname);
  }

  safeFilesContainer.innerHTML = "";
  if (!cachedSafeFolders || cachedSafeFolders.length === 0) {
    renderSafeEmptyState("No folders have been added to the Safe List on the host PC yet.");
    return;
  }

  cachedSafeFolders.forEach((f) => {
    const card = document.createElement("div");
    card.className = "file-card safelist-root-folder";

    const top = document.createElement("div");
    top.className = "file-card-top";

    const iconDiv = document.createElement("div");
    iconDiv.className = "file-icon folder";
    iconDiv.innerHTML = categoryIcons.folder;

    const details = document.createElement("div");
    details.className = "file-details";

    const name = document.createElement("div");
    name.className = "file-name";
    name.title = f.name;
    name.innerText = f.name;

    const meta = document.createElement("div");
    meta.className = "file-meta";
    meta.innerText = `${f.item_count || 0} items • Shared PC Folder`;

    details.appendChild(name);
    details.appendChild(meta);
    top.appendChild(iconDiv);
    top.appendChild(details);
    card.appendChild(top);

    const actions = document.createElement("div");
    actions.className = "file-card-actions";
    const openBtn = document.createElement("button");
    openBtn.className = "card-action-btn";
    openBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg><span>Browse Folder</span>`;
    actions.appendChild(openBtn);
    card.appendChild(actions);

    card.addEventListener("click", () => {
      selectSafeFolder.value = f.id;
      browseSafeFolder(f.id, "");
    });

    safeFilesContainer.appendChild(card);
  });
}

async function fetchSafeFolders() {
  try {
    const res = await fetch("/api/safelist");
    if (!res.ok) return;
    const data = await res.json();
    cachedSafeFolders = data.folders || [];

    badgeSafeCount.innerText = cachedSafeFolders.length;
    selectSafeFolder.innerHTML = "";

    if (cachedSafeFolders.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.innerText = "No folders added to Safe List on PC";
      selectSafeFolder.appendChild(opt);
      renderSafeEmptyState("No folders have been added to the Safe List on the host PC yet.");
      if (btnSafeBack) btnSafeBack.style.display = "none";
      return;
    }

    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.innerText = "📁 All Shared Safe Folders";
    selectSafeFolder.appendChild(allOpt);

    cachedSafeFolders.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.id;
      opt.innerText = `📂 ${f.name} (${f.item_count} items)`;
      selectSafeFolder.appendChild(opt);
    });

    // Auto-select folder or show root list
    if (!currentFolderId) {
      if (cachedSafeFolders.length === 1) {
        currentFolderId = cachedSafeFolders[0].id;
        currentSubpath = "";
        selectSafeFolder.value = currentFolderId;
        browseSafeFolder(currentFolderId, "", false);
      } else {
        showAllSafeFolders(false);
      }
    } else {
      selectSafeFolder.value = currentFolderId;
      browseSafeFolder(currentFolderId, currentSubpath, false);
    }
  } catch (e) {
    renderSafeEmptyState("Error loading safe list folders.");
  }
}

selectSafeFolder.addEventListener("change", (e) => {
  currentFolderId = e.target.value;
  currentSubpath = "";
  if (currentFolderId) {
    browseSafeFolder(currentFolderId, "");
  } else {
    showAllSafeFolders();
  }
});

btnRefreshSafe.addEventListener("click", () => {
  if (currentFolderId) {
    browseSafeFolder(currentFolderId, currentSubpath);
  } else {
    fetchSafeFolders();
  }
});

// --- Safe List Folder ZIP Packaging with Live Progress Circle ---

btnDownloadFolderZip.addEventListener("click", () => {
  if (!currentFolderId) return;
  startFolderZipDownload(currentFolderId, currentSubpath);
});

function openZipProgressModal(folderName) {
  if (zipModalFolderText) zipModalFolderText.innerText = folderName;
  if (zipProgressCircle) {
    zipProgressCircle.style.strokeDashoffset = ZIP_RING_CIRCUMFERENCE;
    zipProgressCircle.classList.remove("completed", "error");
  }
  if (zipPercentLabel) zipPercentLabel.innerText = "0%";
  if (zipStatusBadge) {
    zipStatusBadge.innerText = "Packaging";
    zipStatusBadge.style.color = "var(--accent-cyan)";
  }
  if (zipCurrentFile) zipCurrentFile.innerText = "Scanning folder contents...";
  if (zipFilesCount) zipFilesCount.innerText = "0 / 0 files";
  if (zipBytesCount) zipBytesCount.innerText = "0 B / 0 B";
  if (btnCancelZipLabel) btnCancelZipLabel.innerText = "Cancel";

  if (zipProgressModal) {
    zipProgressModal.classList.remove("hidden");
  }
}

function closeZipProgressModal() {
  if (zipProgressTimer) {
    clearInterval(zipProgressTimer);
    zipProgressTimer = null;
  }
  activeZipJobId = null;
  if (zipProgressModal) {
    zipProgressModal.classList.add("hidden");
  }
}

async function cancelZipDownload() {
  if (zipProgressTimer) {
    clearInterval(zipProgressTimer);
    zipProgressTimer = null;
  }
  if (activeZipJobId) {
    const jid = activeZipJobId;
    activeZipJobId = null;
    try {
      await fetch("/api/safelist/cancel-zip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jid }),
      });
    } catch (e) {
      // Ignore network errors on cancel
    }
  }
  closeZipProgressModal();
  showToast("ZIP preparation cancelled", "info");
}

async function startFolderZipDownload(folderId, subpath) {
  let folderName = "Folder";
  if (subpath) {
    folderName = subpath.split(/[\\/]/).filter(Boolean).pop() || "Folder";
  } else {
    const match = cachedSafeFolders.find((f) => f.id === folderId);
    if (match && match.name) folderName = match.name;
  }

  openZipProgressModal(folderName);

  try {
    const res = await fetch("/api/safelist/prepare-zip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id: folderId, subpath: subpath }),
    });

    const data = await res.json();
    if (!res.ok || !data.success) {
      showZipError(data.error || "Failed to start packaging folder");
      return;
    }

    activeZipJobId = data.job_id;
    if (data.total_files === 0) {
      if (zipCurrentFile) zipCurrentFile.innerText = "Folder is empty. Preparing empty ZIP...";
    } else {
      if (zipFilesCount) zipFilesCount.innerText = `0 / ${data.total_files} files`;
      if (zipBytesCount) zipBytesCount.innerText = `0 B / ${formatBytes(data.total_bytes)}`;
    }

    pollZipProgress(data.job_id, folderName, data.total_files, data.total_bytes);
  } catch (err) {
    showZipError("Network error starting ZIP preparation");
  }
}

function showZipError(message) {
  if (zipProgressTimer) {
    clearInterval(zipProgressTimer);
    zipProgressTimer = null;
  }
  if (zipProgressCircle) zipProgressCircle.classList.add("error");
  if (zipPercentLabel) zipPercentLabel.innerText = "!";
  if (zipStatusBadge) {
    zipStatusBadge.innerText = "Error";
    zipStatusBadge.style.color = "var(--accent-rose)";
  }
  if (zipCurrentFile) zipCurrentFile.innerText = message;
  if (btnCancelZipLabel) btnCancelZipLabel.innerText = "Close";
  showToast(message, "error");
}

function pollZipProgress(jobId, folderName, initialFiles, initialBytes) {
  if (zipProgressTimer) clearInterval(zipProgressTimer);

  zipProgressTimer = setInterval(async () => {
    if (!activeZipJobId || activeZipJobId !== jobId) {
      clearInterval(zipProgressTimer);
      return;
    }

    try {
      const res = await fetch(`/api/safelist/zip-progress?job_id=${encodeURIComponent(jobId)}`);
      if (!res.ok) {
        clearInterval(zipProgressTimer);
        showZipError("Unable to track compression progress");
        return;
      }

      const progress = await res.json();

      if (progress.status === "processing") {
        const pct = Math.min(99.0, Math.max(0, progress.percent || 0));
        const offset = ZIP_RING_CIRCUMFERENCE - (pct / 100) * ZIP_RING_CIRCUMFERENCE;

        if (zipProgressCircle) zipProgressCircle.style.strokeDashoffset = offset;
        if (zipPercentLabel) zipPercentLabel.innerText = `${Math.floor(pct)}%`;
        if (zipStatusBadge) {
          zipStatusBadge.innerText = "Packaging";
          zipStatusBadge.style.color = "var(--accent-cyan)";
        }
        if (zipCurrentFile) {
          zipCurrentFile.innerText = progress.current_file
            ? `Packing: ${progress.current_file}`
            : "Compressing files...";
          zipCurrentFile.title = progress.current_file || "";
        }
        if (zipFilesCount) {
          zipFilesCount.innerText = `${progress.processed_files} / ${progress.total_files || initialFiles} files`;
        }
        if (zipBytesCount) {
          zipBytesCount.innerText = `${formatBytes(progress.processed_bytes)} / ${formatBytes(progress.total_bytes || initialBytes)}`;
        }
      } else if (progress.status === "completed") {
        clearInterval(zipProgressTimer);
        zipProgressTimer = null;

        if (zipProgressCircle) {
          zipProgressCircle.style.strokeDashoffset = 0;
          zipProgressCircle.classList.remove("error");
          zipProgressCircle.classList.add("completed");
        }
        if (zipPercentLabel) zipPercentLabel.innerText = "100%";
        if (zipStatusBadge) {
          zipStatusBadge.innerText = "Ready";
          zipStatusBadge.style.color = "var(--accent-emerald)";
        }
        if (zipCurrentFile) zipCurrentFile.innerText = "Archive ready! Starting download...";
        if (zipFilesCount) {
          zipFilesCount.innerText = `${progress.total_files || initialFiles} / ${progress.total_files || initialFiles} files`;
        }
        if (zipBytesCount) {
          zipBytesCount.innerText = `${formatBytes(progress.total_bytes || initialBytes)} / ${formatBytes(progress.total_bytes || initialBytes)}`;
        }

        // Trigger browser file download
        const downloadUrl = `/api/safelist/download-zip-file?job_id=${encodeURIComponent(jobId)}`;
        const downloadLink = document.createElement("a");
        downloadLink.href = downloadUrl;
        downloadLink.download = `${folderName}.zip`;
        document.body.appendChild(downloadLink);
        downloadLink.click();
        downloadLink.remove();

        showToast(`✓ ZIP download started: ${folderName}.zip`, "success");

        // Automatically close modal after 1.8 seconds
        setTimeout(() => {
          if (activeZipJobId === jobId) {
            closeZipProgressModal();
          }
        }, 1800);
      } else if (progress.status === "error") {
        clearInterval(zipProgressTimer);
        zipProgressTimer = null;
        showZipError(progress.error || "ZIP packaging failed");
      } else if (progress.status === "cancelled") {
        clearInterval(zipProgressTimer);
        zipProgressTimer = null;
        closeZipProgressModal();
      }
    } catch (e) {
      console.warn("Error polling zip progress:", e);
    }
  }, 300);
}

if (btnSafeBack) {
  btnSafeBack.addEventListener("click", () => {
    if (currentSubpath) {
      browseSafeFolder(currentFolderId, getParentSubpath(currentSubpath));
    } else {
      showAllSafeFolders();
    }
  });
}

// Browser back/forward navigation support
window.addEventListener("popstate", (e) => {
  if (e.state) {
    if (e.state.view === "safelist_folder" && e.state.folderId) {
      browseSafeFolder(e.state.folderId, e.state.subpath || "", false);
    } else if (e.state.view === "safelist_root") {
      showAllSafeFolders(false);
    }
  }
});

async function browseSafeFolder(folderId, subpath = "", pushHistory = true) {
  if (!folderId) {
    showAllSafeFolders(pushHistory);
    return;
  }

  currentFolderId = folderId;
  currentSubpath = subpath;
  if (selectSafeFolder) selectSafeFolder.value = folderId;
  if (btnDownloadFolderZip) btnDownloadFolderZip.style.display = "inline-flex";

  // Update Back button state
  if (btnSafeBack) {
    btnSafeBack.style.display = "inline-flex";
    if (subpath) {
      safeBackBtnLabel.innerText = "Back";
      btnSafeBack.title = "Go up to parent directory";
    } else {
      safeBackBtnLabel.innerText = "All Folders";
      btnSafeBack.title = "Back to all shared safe folders";
    }
  }

  if (pushHistory) {
    window.history.pushState({ view: "safelist_folder", folderId, subpath }, "", window.location.pathname);
  }

  try {
    const url = `/api/safelist/browse?folder_id=${encodeURIComponent(folderId)}&subpath=${encodeURIComponent(subpath)}`;
    const res = await fetch(url);
    if (!res.ok) {
      const err = await res.json();
      renderSafeEmptyState(err.error || "Unable to open folder");
      return;
    }

    const data = await res.json();
    renderBreadcrumbs(data.breadcrumbs || []);
    safeItemsCache = data.items || [];
    renderSafeItems(safeItemsCache);
  } catch (e) {
    renderSafeEmptyState("Error browsing folder: " + e.message);
  }
}

function renderBreadcrumbs(crumbs) {
  breadcrumbBar.innerHTML = "";

  // Root Safe List item
  const rootItem = document.createElement("span");
  rootItem.className = "breadcrumb-item";
  rootItem.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:14px;height:14px;vertical-align:-2px;margin-right:4px;"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>Safe List`;
  rootItem.title = "View all shared safe folders";
  rootItem.addEventListener("click", () => {
    showAllSafeFolders();
  });
  breadcrumbBar.appendChild(rootItem);

  crumbs.forEach((crumb, idx) => {
    const sep = document.createElement("span");
    sep.className = "breadcrumb-separator";
    sep.innerText = "/";
    breadcrumbBar.appendChild(sep);

    const item = document.createElement("span");
    const isLast = (idx === crumbs.length - 1);
    item.className = "breadcrumb-item" + (isLast ? " active" : "");
    item.innerText = crumb.name;
    if (!isLast) {
      item.addEventListener("click", () => {
        browseSafeFolder(currentFolderId, crumb.subpath);
      });
    }
    breadcrumbBar.appendChild(item);
  });
}

function renderSafeItems(items) {
  safeFilesContainer.innerHTML = "";

  // Add Parent Directory item at top if inside a subfolder
  if (currentSubpath) {
    const parentCard = document.createElement("div");
    parentCard.className = "file-card parent-dir-card";

    const top = document.createElement("div");
    top.className = "file-card-top";

    const iconDiv = document.createElement("div");
    iconDiv.className = "file-icon folder parent-icon";
    iconDiv.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 14 4 9 9 4"></polyline><path d="M20 20v-7a4 4 0 0 0-4-4H4"></path></svg>`;

    const details = document.createElement("div");
    details.className = "file-details";

    const name = document.createElement("div");
    name.className = "file-name";
    name.innerText = ".. (Parent Folder)";

    const meta = document.createElement("div");
    meta.className = "file-meta";
    meta.innerText = "Tap to go back one level";

    details.appendChild(name);
    details.appendChild(meta);
    top.appendChild(iconDiv);
    top.appendChild(details);
    parentCard.appendChild(top);

    const actions = document.createElement("div");
    actions.className = "file-card-actions";
    const backBtn = document.createElement("button");
    backBtn.className = "card-action-btn back-action-btn";
    backBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"></polyline></svg><span>Back</span>`;
    actions.appendChild(backBtn);
    parentCard.appendChild(actions);

    parentCard.addEventListener("click", () => {
      browseSafeFolder(currentFolderId, getParentSubpath(currentSubpath));
    });

    safeFilesContainer.appendChild(parentCard);
  }

  if (items.length === 0 && !currentSubpath) {
    renderSafeEmptyState("This folder is empty");
    return;
  }

  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "file-card";

    const top = document.createElement("div");
    top.className = "file-card-top";

    const iconDiv = document.createElement("div");
    iconDiv.className = `file-icon ${item.category || "file"}`;
    iconDiv.innerHTML = categoryIcons[item.category] || categoryIcons.file;

    const details = document.createElement("div");
    details.className = "file-details";

    const name = document.createElement("div");
    name.className = "file-name";
    name.title = item.name;
    name.innerText = item.name;

    const meta = document.createElement("div");
    meta.className = "file-meta";
    meta.innerText = item.is_dir ? "Folder" : `${item.formatted_size} • ${item.modified}`;

    details.appendChild(name);
    details.appendChild(meta);
    top.appendChild(iconDiv);
    top.appendChild(details);
    card.appendChild(top);

    // Actions
    const actions = document.createElement("div");
    actions.className = "file-card-actions";

    if (item.is_dir) {
      const openBtn = document.createElement("button");
      openBtn.className = "card-action-btn";
      openBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg><span>Open Folder</span>`;
      actions.appendChild(openBtn);

      card.addEventListener("click", () => {
        browseSafeFolder(currentFolderId, item.subpath);
      });
    } else {
      // Preview button for media/documents
      const previewBtn = document.createElement("button");
      previewBtn.className = "card-action-btn";
      previewBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg><span>Preview</span>`;
      previewBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openPreviewModal(item.name, item.category, `/api/safelist/preview?folder_id=${encodeURIComponent(currentFolderId)}&subpath=${encodeURIComponent(item.subpath)}`, `/api/safelist/download?folder_id=${encodeURIComponent(currentFolderId)}&subpath=${encodeURIComponent(item.subpath)}`);
      });
      actions.appendChild(previewBtn);

      // Download button
      const dlBtn = document.createElement("button");
      dlBtn.className = "card-action-btn";
      dlBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg><span>Download</span>`;
      dlBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        window.location.href = `/api/safelist/download?folder_id=${encodeURIComponent(currentFolderId)}&subpath=${encodeURIComponent(item.subpath)}`;
      });
      actions.appendChild(dlBtn);

      card.addEventListener("click", () => {
        openPreviewModal(item.name, item.category, `/api/safelist/preview?folder_id=${encodeURIComponent(currentFolderId)}&subpath=${encodeURIComponent(item.subpath)}`, `/api/safelist/download?folder_id=${encodeURIComponent(currentFolderId)}&subpath=${encodeURIComponent(item.subpath)}`);
      });
    }

    card.appendChild(actions);
    safeFilesContainer.appendChild(card);
  });
}

function renderSafeEmptyState(msg) {
  safeFilesContainer.innerHTML = `
    <div class="empty-state">
      <div class="empty-state-icon-wrap">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
        </svg>
      </div>
      <p>${msg}</p>
    </div>
  `;
}

// Search Filter
safeSearchInput.addEventListener("input", (e) => {
  const query = e.target.value.toLowerCase().trim();
  if (!query) {
    renderSafeItems(safeItemsCache);
    return;
  }
  const filtered = safeItemsCache.filter((item) => item.name.toLowerCase().includes(query));
  renderSafeItems(filtered);
});

// --- Send to PC (Auto-Save Upload) ---

function setupUploadEvents() {
  const btnSelectMedia = document.getElementById("btnSelectMedia");
  const mediaInput = document.getElementById("mediaInput");

  if (btnSelectMedia && mediaInput) {
    btnSelectMedia.addEventListener("click", () => mediaInput.click());
    mediaInput.addEventListener("change", () => {
      if (mediaInput.files.length > 0) {
        uploadFiles(Array.from(mediaInput.files));
        mediaInput.value = "";
      }
    });
  }

  btnSelectFiles.addEventListener("click", () => fileInput.click());
  if (btnSelectFolder) {
    btnSelectFolder.addEventListener("click", () => folderInput.click());
  }

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length > 0) {
      uploadFiles(Array.from(fileInput.files));
      fileInput.value = "";
    }
  });

  folderInput.addEventListener("change", () => {
    if (folderInput.files.length > 0) {
      const files = Array.from(folderInput.files);
      // Preserve the picked folder's structure using each file's relative path.
      const dirs = files.map((f) => {
        const rel = f.webkitRelativePath || "";
        const slash = rel.lastIndexOf("/");
        return slash > 0 ? rel.slice(0, slash) : "";
      });
      uploadFiles(files, dirs);
      folderInput.value = "";
    }
  });

  // Drag & Drop
  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("drag-over");
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("drag-over");
    });
  });

  
    dropzone.addEventListener("drop", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("drag-over");
      const dt = e.dataTransfer;
      if (dt && dt.items && dt.items.length > 0) {
        let files = [];
        let dirs = [];
        for (let i = 0; i < dt.items.length; i++) {
          const item = dt.items[i];
          if (item.kind === 'file') {
            if (typeof item.webkitGetAsEntry === 'function') {
              const entry = item.webkitGetAsEntry();
              if (entry) {
                await traverseFileTree(entry, "", files, dirs);
              }
            } else {
              files.push(item.getAsFile());
              dirs.push("");
            }
          }
        }
        if (files.length > 0) {
          uploadFiles(files, dirs);
        }
      } else if (dt && dt.files && dt.files.length > 0) {
        uploadFiles(Array.from(dt.files), new Array(dt.files.length).fill(""));
      }
    });

  function traverseFileTree(item, path, fileArr, dirArr) {
    return new Promise((resolve) => {
      if (item.isFile) {
        item.file((file) => {
          fileArr.push(file);
          dirArr.push(path);
          resolve();
        });
      } else if (item.isDirectory) {
        const dirReader = item.createReader();
        const promises = [];
        const readEntries = () => {
          dirReader.readEntries((entries) => {
            if (entries.length === 0) {
              Promise.all(promises).then(resolve);
            } else {
              for (let i = 0; i < entries.length; i++) {
                promises.push(traverseFileTree(entries[i], path + item.name + "/", fileArr, dirArr));
              }
              readEntries();
            }
          });
        };
        readEntries();
      }
    });
  }

}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
}

// --- Multi-Host Mesh Node Management ---

async function fetchMeshNodes() {
  try {
    const res = await fetch("/api/mesh/nodes");
    if (!res.ok) return;
    const data = await res.json();
    meshNodes = data.nodes || [];
    renderMeshNodesUI(data.role, meshNodes);
  } catch (e) {
    console.error("Error fetching mesh nodes:", e);
  }
}

function renderMeshNodesUI(role, nodes) {
  if (!meshClusterPill || !meshDestinationBar || !destinationPills) return;

  if (nodes && nodes.length > 1) {
    meshClusterPill.style.display = "inline-flex";
    meshClusterText.innerText = `${nodes.length} Hosts Connected`;
    meshDestinationBar.style.display = "flex";

    destinationPills.innerHTML = "";

    nodes.forEach((node) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const isSelected =
        selectedTargetNodeId === node.id ||
        (selectedTargetNodeId === "self" && node.is_self);
      btn.className = "dest-pill" + (isSelected ? " active" : "");

      const icon = node.name.toLowerCase().includes("laptop") ? "💻" : "🖥️";
      const badgeText = node.is_self
        ? node.is_leader
          ? "Primary Host"
          : "This Host"
        : node.is_leader
        ? "Leader"
        : "Satellite";

      btn.innerHTML = `<span>${icon} ${node.name}</span> <span class="dest-badge">${badgeText}</span>`;
      btn.addEventListener("click", () => {
        selectedTargetNodeId = node.id;
        document
          .querySelectorAll(".dest-pill")
          .forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        showToast(`Send destination set to: ${node.name}`, "info");
      });
      destinationPills.appendChild(btn);
    });

    // "All Hosts" Multi-Cast pill
    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className =
      "dest-pill" + (selectedTargetNodeId === "all" ? " active" : "");
    allBtn.innerHTML = `<span>🌐 All Hosts</span> <span class="dest-badge">Multi-Cast</span>`;
    allBtn.addEventListener("click", () => {
      selectedTargetNodeId = "all";
      document
        .querySelectorAll(".dest-pill")
        .forEach((p) => p.classList.remove("active"));
      allBtn.classList.add("active");
      showToast("Send destination set to: ALL connected hosts", "info");
    });
    destinationPills.appendChild(allBtn);
  } else {
    meshClusterPill.style.display = "none";
    meshDestinationBar.style.display = "none";
    selectedTargetNodeId = "self";
  }
}


// --- Transfer Manager ---
class TransferManager {
  constructor() {
    this.queue = [];
    this.activeTransfers = new Map();
    this.container = document.getElementById("transferManagerCard");
    this.list = document.getElementById("activeTransfersList");
    // Adaptive concurrency: small files (photos) ride a wide parallel lane,
    // large files get a narrow lane so they don't clog the Wi-Fi pipe.
    // There is no cap on how many files may be queued.
    this.smallFileThreshold = 8 * 1024 * 1024; // 8 MB
    this.maxSmallConcurrent = 8;
    this.maxLargeConcurrent = 2;
    
    this.queueHeader = document.createElement("div");
    this.queueHeader.className = "queue-header";
    this.queueHeader.style.marginBottom = "10px";
    this.queueHeader.style.fontSize = "0.9rem";
    this.queueHeader.style.color = "var(--text-dim)";
    this.queueHeader.style.display = "none";
    if (this.container && this.list) {
      this.container.insertBefore(this.queueHeader, this.list);
    }
  }

  addTransfer(sessionId, filename, file, uploadUrl, targetName, relativeDir) {
    if (this.container && this.container.classList.contains("hidden")) {
      this.container.classList.remove("hidden");
    }

    const transfer = {
      sessionId, filename, file, uploadUrl, targetName, relativeDir,
      chunkSize: 50 * 1024 * 1024,
      totalChunks: Math.ceil(file.size / (50 * 1024 * 1024)) || 1,
      currentChunk: 0,
      paused: false,
      cancelled: false,
      startTime: 0,
      xhr: null,
      card: null,
      lastUpdate: 0,
      lastSpeedUpdate: 0,
      lastLoaded: 0
    };
    transfer.isSmall = file.size < this.smallFileThreshold;
    myUploadSessions.add(sessionId);

    this.queue.push(transfer);
    this.updateQueueHeader();
    this.processQueue();
  }

  updateQueueHeader() {
    const pending = this.queue.length;
    if (pending > 0) {
      this.queueHeader.innerText = `${pending} file(s) waiting in queue...`;
      this.queueHeader.style.display = "block";
    } else {
      this.queueHeader.style.display = "none";
    }
  }

  processQueue() {
    while (this.queue.length > 0) {
      let activeSmall = 0;
      let activeLarge = 0;
      for (const t of this.activeTransfers.values()) {
        if (t.isSmall) activeSmall++;
        else activeLarge++;
      }
      const smallRoom = activeSmall < this.maxSmallConcurrent;
      const largeRoom = activeLarge < this.maxLargeConcurrent;
      if (!smallRoom && !largeRoom) break;

      // Start the next queued file whose lane has room (small files may
      // jump ahead of blocked large ones, and vice versa).
      let idx = -1;
      for (let i = 0; i < this.queue.length; i++) {
        if ((this.queue[i].isSmall && smallRoom) || (!this.queue[i].isSmall && largeRoom)) {
          idx = i;
          break;
        }
      }
      if (idx === -1) break;

      const t = this.queue.splice(idx, 1)[0];
      this.updateQueueHeader();
      this.startTransfer(t);
    }
  }

  startTransfer(t) {
    this.activeTransfers.set(t.sessionId, t);
    
    const card = document.createElement("div");
    card.className = "transfer-item";
    card.id = `transfer-${t.sessionId}`;
    
    card.innerHTML = `
      <div class="transfer-info">
        <div class="transfer-name">${t.filename} ${t.relativeDir ? '('+t.relativeDir+')' : ''}</div>
        <div class="transfer-stats">
          <span id="speed-${t.sessionId}">0 MB/s</span>
          <span id="eta-${t.sessionId}">ETA: ...</span>
        </div>
      </div>
      <div class="transfer-progress">
        <div class="transfer-bar-bg">
          <div class="transfer-bar-fill" id="bar-${t.sessionId}"></div>
        </div>
        <div class="transfer-actions">
          <button id="pause-${t.sessionId}" class="btn-pause">Pause</button>
          <button id="cancel-${t.sessionId}" class="btn-cancel">Cancel</button>
        </div>
      </div>
    `;
    this.list.appendChild(card);
    
    t.card = card;
    t.speedEl = card.querySelector(`#speed-${t.sessionId}`);
    t.etaEl = card.querySelector(`#eta-${t.sessionId}`);
    t.barEl = card.querySelector(`#bar-${t.sessionId}`);
    t.pauseBtn = card.querySelector(`#pause-${t.sessionId}`);
    t.cancelBtn = card.querySelector(`#cancel-${t.sessionId}`);
    
    t.pauseBtn.addEventListener("click", () => this.togglePause(t.sessionId));
    t.cancelBtn.addEventListener("click", () => this.cancelTransfer(t.sessionId));
    
    t.startTime = Date.now();
    this.uploadNextChunk(t.sessionId);
  }

  togglePause(sessionId) {
    const t = this.activeTransfers.get(sessionId);
    if (!t) return;
    t.paused = !t.paused;
    t.pauseBtn.innerText = t.paused ? "Resume" : "Pause";
    if (t.paused && t.xhr) {
      t.xhr.abort();
    } else if (!t.paused) {
      this.uploadNextChunk(sessionId);
    }
  }

  cancelTransfer(sessionId) {
    myUploadSessions.delete(sessionId);
    const t = this.activeTransfers.get(sessionId);
    if (t) {
      t.cancelled = true;
      if (t.xhr) t.xhr.abort();
      if (t.card) t.card.remove();
      this.activeTransfers.delete(sessionId);
    } else {
      this.queue = this.queue.filter(x => x.sessionId !== sessionId);
      this.updateQueueHeader();
    }
    this.checkIfDone();
  }

  checkIfDone() {
    if (this.activeTransfers.size === 0 && this.queue.length === 0) {
      if (this.container) this.container.classList.add("hidden");
    } else {
      this.processQueue();
    }
  }

  uploadNextChunk(sessionId) {
    const t = this.activeTransfers.get(sessionId);
    if (!t || t.paused || t.cancelled) return;

    const start = t.currentChunk * t.chunkSize;
    const end = Math.min(start + t.chunkSize, t.file.size);
    const chunk = t.file.slice(start, end);

    t.xhr = new XMLHttpRequest();
    const chunkUrl = t.uploadUrl.replace("/api/upload", "/api/upload/chunk");
    t.xhr.open("POST", chunkUrl);
    t.xhr.setRequestHeader("Content-Type", "application/octet-stream");
    t.xhr.setRequestHeader("X-Session-Id", t.sessionId);
    t.xhr.setRequestHeader("X-Filename", encodeURIComponent(t.filename));
    t.xhr.setRequestHeader("X-Chunk-Index", t.currentChunk.toString());
    t.xhr.setRequestHeader("X-Total-Chunks", t.totalChunks.toString());
    if (t.relativeDir) {
      t.xhr.setRequestHeader("X-Relative-Dir", encodeURIComponent(t.relativeDir));
    }
    
    if (!t.lastSpeedUpdate) t.lastSpeedUpdate = Date.now();
    if (!t.lastLoaded) t.lastLoaded = start;

    t.xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        const now = Date.now();
        const totalLoaded = start + e.loaded;
        
        if (now - t.lastUpdate > 200) {
          t.lastUpdate = now;
          const pct = Math.round((totalLoaded / t.file.size) * 100);
          t.barEl.style.width = pct + "%";
          
          const elapsedSec = (now - t.lastSpeedUpdate) / 1000;
          if (elapsedSec >= 0.5) {
            const bytesSinceLast = totalLoaded - t.lastLoaded;
            const bps = bytesSinceLast / elapsedSec;
            t.speedEl.innerText = `${formatBytes(bps)}/s`;
            
            const remaining = t.file.size - totalLoaded;
            const etaSec = bps > 0 ? Math.round(remaining / bps) : 0;
            t.etaEl.innerText = `ETA: ${etaSec}s`;
            
            t.lastSpeedUpdate = now;
            t.lastLoaded = totalLoaded;
          }
        }
      }
    });

    t.xhr.addEventListener("load", () => {
      if (t.xhr.status >= 200 && t.xhr.status < 300) {
        const resp = JSON.parse(t.xhr.responseText);
        if (resp.completed) {
          if (t.card) t.card.remove();
          this.activeTransfers.delete(sessionId);
          myUploadSessions.delete(sessionId);
          // Keep the queue draining first; UI updates below must never
          // be able to stall the next transfer if one of them throws.
          this.checkIfDone();
          try {
            showToast(`Uploaded ${t.filename}`, "success");
            if (resp.file) addSentHistoryItem(resp.file);
            if (typeof fetchReceivedFiles === "function") fetchReceivedFiles();
          } catch (e) {
            console.error("Post-upload UI update failed:", e);
          }
        } else {
          t.currentChunk++;
          this.uploadNextChunk(sessionId);
        }
      } else {
        if (t.speedEl) t.speedEl.innerText = "Error";
        if (t.speedEl) t.speedEl.style.color = "red";
        t.paused = true;
        if (t.pauseBtn) t.pauseBtn.innerText = "Retry";
      }
    });

    t.xhr.addEventListener("error", () => {
      if (t.speedEl) t.speedEl.innerText = "Error";
      if (t.speedEl) t.speedEl.style.color = "red";
      t.paused = true;
      if (t.pauseBtn) t.pauseBtn.innerText = "Retry";
    });

    t.xhr.send(chunk);
  }
}
const transferManager = new TransferManager();

function uuidv4() {
  return "10000000-1000-4000-8000-100000000000".replace(/[018]/g, c =>
    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
  );
}

// --- Send Files (Auto-Save Upload) ---

function uploadFiles(files, relativeDirs = []) {
  if (!files || files.length === 0) return;

  let targets = [];
  if (selectedTargetNodeId === "all" && meshNodes.length > 0) {
    targets = meshNodes.map((n) => ({ url: `${n.url}/api/upload`, name: n.name }));
  } else if (selectedTargetNodeId !== "self" && meshNodes.length > 0) {
    const targetNode = meshNodes.find((n) => n.id === selectedTargetNodeId);
    if (targetNode) {
      targets = [{ url: `${targetNode.url}/api/upload`, name: targetNode.name }];
    } else {
      targets = [{ url: "/api/upload", name: "host PC" }];
    }
  } else {
    targets = [{ url: "/api/upload", name: "host PC" }];
  }

  targets.forEach((target) => {
    files.forEach((file, idx) => {
      const sessionId = uuidv4();
      const relDir = relativeDirs[idx] || "";
      transferManager.addTransfer(sessionId, file.name, file, target.url, target.name, relDir);
    });
  });
}


function addSentHistoryItem(fileInfo) {
  if (!sentList) return;
  const emptySent = sentList.querySelector(".empty-sent");
  if (emptySent) emptySent.remove();

  const item = document.createElement("div");
  item.className = "sent-item";
  item.innerHTML = `
    <div class="sent-item-left">
      <span class="sent-check">✓</span>
      <strong>${fileInfo.filename}</strong>
      <span class="file-meta">(${fileInfo.formatted_size})</span>
    </div>
    <span class="file-meta">${fileInfo.timestamp}</span>
  `;
  sentList.prepend(item);
}

// --- Received Files Tab ---

async function fetchReceivedFiles() {
  try {
    const res = await fetch("/api/received");
    if (!res.ok) return;
    const data = await res.json();
    receivedPathDesc.innerText = data.save_directory || "Downloads/NearBeam_Received";
    renderReceivedFiles(data.files || []);
  } catch (e) {
    receivedFilesContainer.innerHTML = `<div class="empty-state"><p>Error loading received files</p></div>`;
  }
}

btnRefreshReceived.addEventListener("click", fetchReceivedFiles);

function renderReceivedFiles(files) {
  receivedFilesContainer.innerHTML = "";

  if (files.length === 0) {
    receivedFilesContainer.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon-wrap">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
            <polyline points="22 12 16 12 14 15 10 15 8 12 2 12"></polyline>
            <path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"></path>
          </svg>
        </div>
        <p>No files currently in PC's auto-save directory</p>
        <span class="empty-state-hint">Files sent from your phone or other devices will show up here automatically.</span>
      </div>
    `;
    return;
  }

  files.forEach((file) => {
    const card = document.createElement("div");
    card.className = "file-card";

    const top = document.createElement("div");
    top.className = "file-card-top";

    const iconDiv = document.createElement("div");
    iconDiv.className = `file-icon ${file.category || "file"}`;
    iconDiv.innerHTML = categoryIcons[file.category] || categoryIcons.file;

    const details = document.createElement("div");
    details.className = "file-details";

    const name = document.createElement("div");
    name.className = "file-name";
    name.title = file.filename;
    name.innerText = file.filename;

    const meta = document.createElement("div");
    meta.className = "file-meta";
    meta.innerText = `${file.formatted_size} • ${file.modified}`;

    details.appendChild(name);
    details.appendChild(meta);
    top.appendChild(iconDiv);
    top.appendChild(details);
    card.appendChild(top);

    const actions = document.createElement("div");
    actions.className = "file-card-actions";

    const dlUrl = `/api/received/download/${encodeURIComponent(file.filename)}`;
    const prevUrl = `/api/received/preview/${encodeURIComponent(file.filename)}`;

    const prevBtn = document.createElement("button");
    prevBtn.className = "card-action-btn";
    prevBtn.innerHTML = `<span>Preview</span>`;
    prevBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      openPreviewModal(file.filename, file.category, prevUrl, dlUrl);
    });
    actions.appendChild(prevBtn);

    const dlBtn = document.createElement("button");
    dlBtn.className = "card-action-btn";
    dlBtn.innerHTML = `<span>Download</span>`;
    dlBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      window.location.href = dlUrl;
    });
    actions.appendChild(dlBtn);

    card.appendChild(actions);
    card.addEventListener("click", () => {
      openPreviewModal(file.filename, file.category, prevUrl, dlUrl);
    });

    receivedFilesContainer.appendChild(card);
  });
}

// --- Shared Clipboard ---

let clipboardDebounceTimer = null;
let isUpdatingFromSSE = false;

function setupClipboardSync() {
  if (!chkKeepSyncingClipboard) return;

  // Restore saved preference (defaults to true)
  const savedAutoSync = localStorage.getItem("nearbeam_keep_syncing_clipboard") || localStorage.getItem("landrop_keep_syncing_clipboard");
  const isAutoSync = savedAutoSync !== null ? savedAutoSync === "1" : true;
  chkKeepSyncingClipboard.checked = isAutoSync;
  updateClipboardSyncUI(isAutoSync);

  chkKeepSyncingClipboard.addEventListener("change", () => {
    const active = chkKeepSyncingClipboard.checked;
    localStorage.setItem("nearbeam_keep_syncing_clipboard", active ? "1" : "0");
    updateClipboardSyncUI(active);

    if (active) {
      // Sync current text immediately on turning on
      if (clipboardTextarea.value.trim()) {
        sendClipboardData(clipboardTextarea.value, false);
      }
      showToast("✓ Live clipboard auto-sync enabled", "info");
    } else {
      showToast("Clipboard auto-sync paused (manual sync mode)", "info");
    }
  });

  // Continuous auto-sync on typing or pasting (no button click needed)
  clipboardTextarea.addEventListener("input", () => {
    if (!chkKeepSyncingClipboard.checked || isUpdatingFromSSE) return;

    if (syncLiveStatus) {
      syncLiveStatus.className = "sync-live-status syncing";
      syncLiveStatus.innerText = "● Syncing...";
    }

    clearTimeout(clipboardDebounceTimer);
    clipboardDebounceTimer = setTimeout(() => {
      sendClipboardData(clipboardTextarea.value, false);
    }, 350);
  });

  // Re-sync whenever user switches back to this browser tab
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && chkKeepSyncingClipboard.checked) {
      fetchClipboard();
    }
  });
}

function updateClipboardSyncUI(active) {
  if (!syncLiveStatus) return;
  if (active) {
    syncLiveStatus.className = "sync-live-status";
    syncLiveStatus.innerText = "● Auto-Sync Active";
    if (btnSendClipboardLabel) btnSendClipboardLabel.innerText = "Synced (Auto)";
  } else {
    syncLiveStatus.className = "sync-live-status disabled";
    syncLiveStatus.innerText = "○ Manual Sync Mode";
    if (btnSendClipboardLabel) btnSendClipboardLabel.innerText = "Sync Now";
  }
}

async function sendClipboardData(text, showToastNotification = true) {
  try {
    const res = await fetch("/api/clipboard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (res.ok) {
      if (syncLiveStatus && chkKeepSyncingClipboard && chkKeepSyncingClipboard.checked) {
        syncLiveStatus.className = "sync-live-status";
        syncLiveStatus.innerText = "● Auto-Sync Active";
      }
      if (showToastNotification) {
        showToast("✓ Clipboard synced to PC & all devices!", "success");
      }
    }
  } catch (e) {
    if (syncLiveStatus) {
      syncLiveStatus.className = "sync-live-status disabled";
      syncLiveStatus.innerText = "⚠️ Sync Error";
    }
    if (showToastNotification) {
      showToast("Failed to sync clipboard", "error");
    }
  }
}

async function fetchClipboard() {
  try {
    const res = await fetch("/api/clipboard");
    if (!res.ok) return;
    const data = await res.json();
    if (data.text !== undefined && data.text !== clipboardTextarea.value) {
      isUpdatingFromSSE = true;
      clipboardTextarea.value = data.text;
      isUpdatingFromSSE = false;
    }
    if (data.updated_at) {
      clipboardUpdatedTime.innerText = `Last synced: ${data.updated_at}`;
    }
  } catch (e) {
    console.error("Clipboard fetch error:", e);
  }
}

btnSendClipboard.addEventListener("click", () => {
  sendClipboardData(clipboardTextarea.value, true);
});

btnCopyClipboard.addEventListener("click", async () => {
  const text = clipboardTextarea.value;
  if (!text) {
    showToast("Clipboard is empty", "info");
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    showToast("✓ Copied to your device's clipboard!", "success");
  } catch (e) {
    // Fallback for older browsers
    clipboardTextarea.select();
    document.execCommand("copy");
    showToast("✓ Copied to clipboard!", "success");
  }
});

// --- Real-Time SSE Setup ---

function setupSSE() {
  if (!window.EventSource) return;

  const eventSource = new EventSource("/api/events");

  eventSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.type === "file_received") {
        removeIncomingProgress(data.session_id);
        showToast(`Incoming file auto-saved: ${data.file.filename}`, "info");
        fetchReceivedFiles();
      } else if (data.type === "upload_progress") {
        // Live progress for a transfer arriving from another device.
        if (!myUploadSessions.has(data.session_id)) {
          renderIncomingProgress(data);
        }
      } else if (data.type === "clipboard_updated") {
        if (data.text !== clipboardTextarea.value) {
          isUpdatingFromSSE = true;
          clipboardTextarea.value = data.text;
          isUpdatingFromSSE = false;
        }
        clipboardUpdatedTime.innerText = `Synced just now (${data.updated_at})`;

        // Auto-copy to device clipboard if keep syncing is enabled and window has focus
        if (chkKeepSyncingClipboard && chkKeepSyncingClipboard.checked) {
          if (navigator.clipboard && document.hasFocus()) {
            navigator.clipboard.writeText(data.text).catch(() => {});
          }
        }
      } else if (data.type === "mesh_nodes_changed") {
        if (data.nodes) {
          meshNodes = data.nodes;
          renderMeshNodesUI(serverInfo ? serverInfo.mesh_role : "leader", meshNodes);
        } else {
          fetchMeshNodes();
        }
      } else if (data.type === "mesh_role_changed") {
        fetchMeshNodes();
      }
    } catch (err) {
      // heartbeats or non-json messages
    }
  };

  eventSource.onerror = () => {
    // Reconnects automatically
  };
}

// --- Incoming Transfers (progress for files arriving from other devices) ---

function renderIncomingProgress(data) {
  if (!incomingTransferCard || !incomingTransfersList || !data.session_id) return;
  incomingTransferCard.classList.remove("hidden");

  const label = data.relative_dir
    ? `${data.relative_dir}/${data.filename}`
    : data.filename || "Incoming file";

  let row = document.getElementById(`incoming-${data.session_id}`);
  if (!row) {
    row = document.createElement("div");
    row.className = "transfer-item";
    row.id = `incoming-${data.session_id}`;
    row.innerHTML = `
      <div class="transfer-info">
        <div class="transfer-name"></div>
        <div class="transfer-stats"><span class="incoming-pct">0%</span></div>
      </div>
      <div class="transfer-progress">
        <div class="transfer-bar-bg"><div class="transfer-bar-fill" style="width:0%"></div></div>
      </div>`;
    // textContent avoids HTML injection via a crafted filename.
    row.querySelector(".transfer-name").textContent = label;
    incomingTransfersList.appendChild(row);
    showToast(`Receiving ${data.filename || "file"}…`, "info");
  }

  const pct = Math.min(100, Math.max(0, data.percent || 0));
  const fill = row.querySelector(".transfer-bar-fill");
  const pctEl = row.querySelector(".incoming-pct");
  if (fill) fill.style.width = pct + "%";
  if (pctEl) pctEl.innerText = `${Math.floor(pct)}% • ${formatBytes(data.received_bytes || 0)}`;
}

function removeIncomingProgress(sessionId) {
  if (!sessionId || !incomingTransfersList) return;
  const row = document.getElementById(`incoming-${sessionId}`);
  if (row) row.remove();
  if (incomingTransferCard && incomingTransfersList.children.length === 0) {
    incomingTransferCard.classList.add("hidden");
  }
}

// --- Modals Setup ---

function setupModalEvents() {
  // Preview Modal
  btnClosePreview.addEventListener("click", () => previewModal.classList.add("hidden"));
  previewModal.addEventListener("click", (e) => {
    if (e.target === previewModal) previewModal.classList.add("hidden");
  });

  // QR Modal
  btnQrModal.addEventListener("click", () => qrModal.classList.remove("hidden"));
  btnCloseQr.addEventListener("click", () => qrModal.classList.add("hidden"));
  qrModal.addEventListener("click", (e) => {
    if (e.target === qrModal) qrModal.classList.add("hidden");
  });

  // ZIP Progress Modal
  if (btnCloseZipModal) btnCloseZipModal.addEventListener("click", cancelZipDownload);
  if (btnCancelZip) btnCancelZip.addEventListener("click", cancelZipDownload);
  if (zipProgressModal) {
    zipProgressModal.addEventListener("click", (e) => {
      if (e.target === zipProgressModal) cancelZipDownload();
    });
  }

  // ESC key closes modals
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      previewModal.classList.add("hidden");
      qrModal.classList.add("hidden");
      if (zipProgressModal && !zipProgressModal.classList.contains("hidden")) {
        cancelZipDownload();
      }
    }
  });
}

async function openPreviewModal(title, category, previewUrl, downloadUrl) {
  previewModalTitle.innerText = title;
  previewModalBody.innerHTML = "<p>Loading preview...</p>";
  btnModalDownload.href = downloadUrl;
  previewModal.classList.remove("hidden");

  if (category === "image") {
    previewModalBody.innerHTML = `<img src="${previewUrl}" alt="${title}" />`;
  } else if (category === "video") {
    previewModalBody.innerHTML = `<video src="${previewUrl}" controls autoplay playsinline></video>`;
  } else if (category === "audio") {
    previewModalBody.innerHTML = `<audio src="${previewUrl}" controls autoplay></audio>`;
  } else if (category === "code" || category === "document") {
    try {
      const res = await fetch(previewUrl);
      const text = await res.text();
      // Show first 50KB if huge
      const snippet = text.length > 50000 ? text.slice(0, 50000) + "\n\n... (file preview truncated)" : text;
      previewModalBody.innerHTML = `<pre><code>${escapeHtml(snippet)}</code></pre>`;
    } catch (e) {
      previewModalBody.innerHTML = `<p>Unable to preview this file inline.</p>`;
    }
  } else {
    previewModalBody.innerHTML = `<p>Preview not available for this file type. Click below to download.</p>`;
  }
}

function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// --- Help & Device FAQ Setup ---

function setupFaqEvents() {
  const faqItems = document.querySelectorAll(".faq-item");
  faqItems.forEach((item) => {
    const question = item.querySelector(".faq-question");
    if (question) {
      question.addEventListener("click", () => {
        item.classList.toggle("open");
      });
    }
  });

  const btnFaqNav = document.getElementById("btnFaqNav");
  if (btnFaqNav) {
    btnFaqNav.addEventListener("click", () => {
      const tabBtns = document.querySelectorAll(".tab-btn");
      const tabPanes = document.querySelectorAll(".tab-pane");
      tabBtns.forEach((b) => b.classList.remove("active"));
      tabPanes.forEach((p) => p.classList.remove("active"));

      const faqBtn = document.querySelector('.tab-btn[data-tab="tab-faq"]');
      const faqPane = document.getElementById("tab-faq");
      if (faqBtn) faqBtn.classList.add("active");
      if (faqPane) {
        faqPane.classList.add("active");
        faqPane.scrollIntoView({ behavior: "smooth" });
      }
    });
  }
}


// --- Guest Drop Zone ---
const btnCreateDropZone = document.getElementById("btnCreateDropZone");
if (btnCreateDropZone) {
  btnCreateDropZone.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/drop/create", { method: "POST" });
      if (!res.ok) {
        showToast("Error creating Drop Zone", "error");
        return;
      }
      const data = await res.json();
      
      // Update QR Modal to show the drop zone URL
      if (qrUrlText) qrUrlText.innerText = data.url;
      // You could theoretically update the QR image src here, but it's fine for now
      // Let's just show a toast and open the QR modal
      showToast("Drop Zone created! Token valid for 24h", "success");
      
      if (qrModal) qrModal.classList.remove("hidden");
    } catch (e) {
      showToast("Failed to create Drop Zone", "error");
    }
  });
}
