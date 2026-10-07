# 📋 NearBeam Product Backlog

A prioritized roadmap of strategic enhancements, UX refinements, and native OS integrations for **NearBeam**.

---

## 🎯 Summary Matrix

| ID | Feature / Enhancement | Story Points | Effort | Impact | Priority | Status |
|:---|:---|:---:|:---:|:---:|:---:|:---:|
| **NB-01** | **Hero Positioning: "Zero QR, Zero Accounts, Zero Cables"** | 1 | Low | High | P1 | Ready |
| **NB-02** | **"Nearby Devices" First-Class Dashboard** | 8 | Med-High | Very High | P1 | Ready |
| **NB-03** | **Production-Grade Transfer Manager (Retry & History)** | 5 | Medium | High | P1 | In Progress |
| **NB-04** | **Global Drag-and-Drop Everywhere** | 3 | Low-Med | High | P2 | Ready |
| **NB-05** | **Windows Shell & Share Menu Integration** | 5 | Medium | High | P2 | Ready |
| **NB-06** | **Close Window Dialog: Minimize to Tray vs. Exit** | 2 | Low | Med-High | P2 | Tracked (#1) |

---

## 📌 Detailed Backlog Items

### NB-01: Hero Positioning & Killer Messaging
- **Theme:** Brand & Copywriting
- **Story Points:** `1 pt` | **Effort:** `Low` | **Impact:** `High`
- **User Story:** As a new user discovering NearBeam, I want immediate clarity on what makes the app superior to AirDrop or cloud drives without technical jargon.
- **Key Deliverables:**
  - Replace generic *"Local file transfer"* slogans with killer value proposition:
    > **"Transfer files without QR codes, accounts, or cables."**
    > **"Open your PC from your phone at nearbeam.local."**
  - Update landing page hero, mobile web app welcome banner, and GitHub meta tags.
- **Acceptance Criteria:**
  - Copy reflects the zero-friction experience across all public facing and in-app headers.

---

### NB-02: "Nearby Devices" Device-First Home Dashboard
- **Theme:** Core UX Redesign
- **Story Points:** `8 pts` | **Effort:** `Medium-High` | **Impact:** `Very High`
- **User Story:** As a multi-device user, I want the home screen to focus on active nearby devices rather than settings, so I can beam files with one tap.
- **Concept Wireframe:**
  ```text
  NEARBEAM
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Nearby Devices

  💻 Gaming-PC
     Online • 2.5 Gbps

  💻 Laptop
     Online • Wi-Fi 6

  📱 iPhone
     Browser connected (Active)
  ─────────────────────────────────
  [ 📤 Send Files ]   [ 📁 Shared Folders ]   [ 📋 Clipboard ]
  ```
- **Key Deliverables:**
  - Move configuration & Safe List management into a secondary tab or modal.
  - Implement active device list populated via mDNS and Mesh cluster heartbeats.
  - Display connection health indicators (IP, connection type, throughput).
  - Quick-action floating bar for Send, Shared Folders, and Clipboard.
- **Acceptance Criteria:**
  - App opens directly to active devices.
  - Dragging a file onto a specific device targets that machine directly.

---

### NB-03: Production-Grade Transfer Manager & Persistent History
- **Theme:** Transfer Reliability & Feedback
- **Story Points:** `5 pts` | **Effort:** `Medium` | **Impact:** `High`
- **User Story:** As a user transferring multiple large files or entire photo albums, I want granular control, visibility, and history so I never wonder if a file completed.
- **Key Deliverables:**
  - **Live Metrics:** Sliding-window speed calculation (MB/s) and dynamic ETA.
  - **Lifecycle Controls:** Pause, Resume, Cancel, and 1-Click Retry on dropped network.
  - **Completed History Log:** Persistent local list of completed transfers with file size, origin device, and timestamp.
  - **Failed Transfers Queue:** Clear error reporting with one-click retry button.
- **Acceptance Criteria:**
  - History persists across browser refreshes using LocalStorage or backend SQLite.
  - Network disconnection triggers auto-retry or clean user-facing retry button.

---

### NB-04: Drag-and-Drop Everywhere
- **Theme:** Seamless Desktop & Web Interaction
- **Story Points:** `3 pts` | **Effort:** `Low-Medium` | **Impact:** `High`
- **User Story:** As a desktop or tablet user, I want to drop files anywhere on the screen without aiming for a tiny dropzone box.
- **Key Deliverables:**
  - Window-wide drag listener with frosted-glass backdrop blur overlay: *"Drop files anywhere to beam"*.
  - Dragging directly onto a device card initiates transfer to that specific device.
  - Recursive folder scanning via HTML5 Drag and Drop API (`webkitGetAsEntry`).
- **Acceptance Criteria:**
  - Dragging anywhere on the browser or desktop window activates the drop target.
  - Dragging entire folders preserves relative directory structure.

---

### NB-05: Windows Native Shell & Share Integration
- **Theme:** OS Deep Integration
- **Story Points:** `5 pts` | **Effort:** `Medium` | **Impact:** `High`
- **User Story:** As a Windows user, I want to right-click files in File Explorer and beam them without manually opening the browser.
- **Key Deliverables:**
  - Add context menu entry: `Right Click -> "Send with NearBeam"`.
  - Windows Share Contract integration (appears in the native Windows 10/11 "Share" menu).
  - Installer checkbox for Explorer shell integration.
- **Acceptance Criteria:**
  - Clicking "Send with NearBeam" launches the app (or opens tray instance) with the target file queued for sending.

---

### NB-06: Window Close Action Dialog (Minimize to Tray vs. Exit)
- **Theme:** Desktop Quality of Life
- **Story Points:** `2 pts` | **Effort:** `Low` | **Impact:** `Medium-High`
- **Reference:** GitHub Issue [#1](https://github.com/tanumay-deb/NearBeam/issues/1)
- **User Story:** As a desktop user, I want to know whether clicking 'X' will kill active transfers or keep the server running in the background.
- **Key Deliverables:**
  - Intercept `closeEvent` in PySide6 `MainWindow`.
  - Show choice dialog:
    1. **Minimize to Tray** *(Keeps Wi-Fi server online for incoming files)*
    2. **Exit NearBeam** *(Completely closes app & server)*
    3. **Cancel**
  - "Remember my choice" option saved in `config.json`.
- **Acceptance Criteria:**
  - Choosing "Minimize to Tray" hides the window to the system notification area without terminating active downloads.
