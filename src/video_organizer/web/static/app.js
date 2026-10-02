const state = {
    config: null,
    tasks: { queued: [], processing: [], completed: [], failed: {} },
    status: null,
    currentTab: 'queued',
    logWebSocket: null,
    currentLogFile: null,
    autoRefresh: null,
    initialized: false,
    recentPage: 1,
    recentSearch: '',
    recentTotal: 0,
    recentPageSize: 20,
    taskPage: 1,
    taskSearch: '',
    taskTotal: 0,
    taskPageSize: 20,
    selectedFiles: []
};

const el = {};

document.addEventListener('DOMContentLoaded', async () => {
    initThemeOnLoginPage();
    bindLoginEvents();
    const authed = await checkAuthApi();
    if (authed) {
        hideLoginPage();
        initApp();
    } else {
        showLoginPage();
    }
});

function bindLoginEvents() {
    const form = document.getElementById('loginForm');
    if (form) form.addEventListener('submit', handleLogin);
    const logoutBtn = document.getElementById('logoutBtn');
    if (logoutBtn) logoutBtn.addEventListener('click', handleLogout);
}

async function initApp() {
    if (state.initialized) return;
    state.initialized = true;
    cacheElements();
    bindEvents();
    await loadInitialData();
    connectDashboardWebSocket();
    startAutoRefresh();
    connectUploadProgressWebSocket();
    initKeyboardShortcuts();
    initMobileMenu();
    initThemeSwitcher();
}

function cacheElements() {
    el.navItems = document.querySelectorAll('.nav-item');
    el.pages = document.querySelectorAll('.page');
    el.statusDot = document.getElementById('statusDot');
    el.statusText = document.getElementById('statusText');
    el.queueCount = document.getElementById('queueCount');
    el.statQueue = document.getElementById('stat-queue');
    el.statProcessing = document.getElementById('stat-processing');
    el.statCompleted = document.getElementById('stat-completed');
    el.statFailed = document.getElementById('stat-failed');
    el.recentActivity = document.getElementById('recent-activity');
    el.taskTabs = document.querySelectorAll('#page-tasks .tab');
    el.taskList = document.getElementById('task-list');
    el.refreshTasksBtn = document.getElementById('refreshTasksBtn');
    el.clearFailedBtn = document.getElementById('clearFailedBtn');
    el.retryAllBtn = document.getElementById('retryAllBtn');
    el.configEditor = document.getElementById('config-editor');
    el.reloadConfigBtn = document.getElementById('reloadConfigBtn');
    el.saveConfigBtn = document.getElementById('saveConfigBtn');
    el.logLevelSelect = document.getElementById('logLevelSelect');
    el.refreshLogBtn = document.getElementById('refreshLogBtn');
    el.logViewer = document.getElementById('logViewer');
    el.autoScrollCheck = document.getElementById('autoScrollCheck');
    el.liveLogCheck = document.getElementById('liveLogCheck');
    el.manualFilePath = document.getElementById('manualFilePath');
    el.browseFileBtn = document.getElementById('browseFileBtn');
    el.forceProcessCheck = document.getElementById('forceProcessCheck');
    el.addToFileListBtn = document.getElementById('addToFileListBtn');
    el.previewFileBtn = document.getElementById('previewFileBtn');
    el.previewResult = document.getElementById('previewResult');
    el.previewContent = document.getElementById('previewContent');
    el.validateScrapeBtn = document.getElementById('validateScrapeBtn');
    el.validateResult = document.getElementById('validateResult');
    el.validateContent = document.getElementById('validateContent');
    el.recursiveScanCheck = document.getElementById('recursiveScanCheck');
    el.scanDirBtn = document.getElementById('scanDirBtn');
    el.fileListPanel = document.getElementById('fileListPanel');
    el.selectedFilesList = document.getElementById('selectedFilesList');
    el.fileListCount = document.getElementById('fileListCount');
    el.selectAllFiles = document.getElementById('selectAllFiles');
    el.processSelectedBtn = document.getElementById('processSelectedBtn');
    el.downloaderList = document.getElementById('downloader-list');
    el.refreshDownloadersBtn = document.getElementById('refreshDownloadersBtn');
    el.downloaderConfigList = document.getElementById('downloader-config-list');
    el.addDownloaderConfigBtn = document.getElementById('addDownloaderConfigBtn');
    el.userList = document.getElementById('user-list');
    el.addUserBtn = document.getElementById('addUserBtn');
    el.uploadProgressList = document.getElementById('upload-progress-list');
    el.uploadStatusDot = document.getElementById('uploadStatusDot');
    el.modalOverlay = document.getElementById('modalOverlay');
    el.modalTitle = document.getElementById('modalTitle');
    el.modalBody = document.getElementById('modalBody');
    el.modalClose = document.getElementById('modalClose');
    el.modalCancelBtn = document.getElementById('modalCancelBtn');
    el.modalConfirmBtn = document.getElementById('modalConfirmBtn');
    el.toastContainer = document.getElementById('toastContainer');
    el.recentSearchInput = document.getElementById('recentSearchInput');
    el.recentSearchBtn = document.getElementById('recentSearchBtn');
    el.recentPrevBtn = document.getElementById('recentPrevBtn');
    el.recentNextBtn = document.getElementById('recentNextBtn');
    el.recentPage = document.getElementById('recentPage');
    el.recentTotalPages = document.getElementById('recentTotalPages');
    el.recentTotal = document.getElementById('recentTotal');
    el.recentPagination = document.getElementById('recentPagination');
    el.recentPageSize = document.getElementById('recentPageSize');
    el.taskSearchInput = document.getElementById('taskSearchInput');
    el.taskSearchBtn = document.getElementById('taskSearchBtn');
    el.taskPrevBtn = document.getElementById('taskPrevBtn');
    el.taskNextBtn = document.getElementById('taskNextBtn');
    el.taskPage = document.getElementById('taskPage');
    el.taskTotalPages = document.getElementById('taskTotalPages');
    el.taskTotal = document.getElementById('taskTotal');
    el.taskPagination = document.getElementById('taskPagination');
    el.taskPageSize = document.getElementById('taskPageSize');
}

function bindEvents() {
    el.navItems.forEach(item => {
        item.addEventListener('click', () => switchPage(item.dataset.page));
    });
    el.taskTabs.forEach(tab => {
        tab.addEventListener('click', () => switchTaskTab(tab.dataset.tab));
    });
    el.refreshTasksBtn.addEventListener('click', loadTasks);
    el.clearFailedBtn.addEventListener('click', () => {
        showConfirm('清除失败记录', '确定要清除所有失败记录吗？此操作不可撤销。', clearAllFailedTasks);
    });
    el.retryAllBtn.addEventListener('click', () => {
        showConfirm('重试全部', '确定要重试所有失败的任务吗？', retryAllFailedTasks);
    });
    el.reloadConfigBtn.addEventListener('click', reloadConfig);
    el.saveConfigBtn.addEventListener('click', saveConfig);
    if (el.logLevelSelect) el.logLevelSelect.addEventListener('change', changeLogLevel);
    if (el.refreshLogBtn) el.refreshLogBtn.addEventListener('click', loadLogContent);
    el.liveLogCheck.addEventListener('change', toggleLiveLog);
    el.browseFileBtn.addEventListener('click', browseFile);
    el.addToFileListBtn.addEventListener('click', addToFileList);
    el.previewFileBtn.addEventListener('click', previewFile);
    el.scanDirBtn.addEventListener('click', scanDirToFileList);
    el.validateScrapeBtn.addEventListener('click', validateScrape);
    el.selectAllFiles.addEventListener('change', toggleSelectAllFiles);
    el.processSelectedBtn.addEventListener('click', processSelectedFiles);
    el.refreshDownloadersBtn.addEventListener('click', loadDownloaders);
    if (el.addDownloaderConfigBtn) el.addDownloaderConfigBtn.addEventListener('click', showAddDownloaderModal);
    if (el.addUserBtn) el.addUserBtn.addEventListener('click', showAddUserModal);
    el.modalClose.addEventListener('click', hideModal);
    el.modalCancelBtn.addEventListener('click', hideModal);
    el.modalOverlay.addEventListener('click', (e) => {
        if (e.target === el.modalOverlay) hideModal();
    });
    // 最近活动分页
    if (el.recentPageSize) el.recentPageSize.addEventListener('change', () => { state.recentPageSize = parseInt(el.recentPageSize.value); state.recentPage = 1; updateRecentActivity(); });
    if (el.recentPrevBtn) el.recentPrevBtn.addEventListener('click', () => { state.recentPage--; updateRecentActivity(); });
    if (el.recentNextBtn) el.recentNextBtn.addEventListener('click', () => { state.recentPage++; updateRecentActivity(); });
    if (el.recentSearchBtn) el.recentSearchBtn.addEventListener('click', () => {
        state.recentSearch = el.recentSearchInput.value.trim();
        state.recentPage = 1;
        updateRecentActivity();
    });
    if (el.recentSearchInput) el.recentSearchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            state.recentSearch = el.recentSearchInput.value.trim();
            state.recentPage = 1;
            updateRecentActivity();
        }
    });
    // 任务列表分页与搜索
    if (el.taskPageSize) el.taskPageSize.addEventListener('change', () => { state.taskPageSize = parseInt(el.taskPageSize.value); state.taskPage = 1; updateTaskList(); });
    if (el.taskPrevBtn) el.taskPrevBtn.addEventListener('click', () => { state.taskPage--; updateTaskList(); });
    if (el.taskNextBtn) el.taskNextBtn.addEventListener('click', () => { state.taskPage++; updateTaskList(); });
    if (el.taskSearchBtn) el.taskSearchBtn.addEventListener('click', () => {
        state.taskSearch = el.taskSearchInput.value.trim();
        state.taskPage = 1;
        updateTaskList();
    });
    if (el.taskSearchInput) el.taskSearchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            state.taskSearch = el.taskSearchInput.value.trim();
            state.taskPage = 1;
            updateTaskList();
        }
    });
}

function initKeyboardShortcuts() {
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') hideModal();
        if ((e.ctrlKey || e.metaKey) && e.key === 'r' && document.querySelector('#page-tasks.active')) {
            e.preventDefault();
            loadTasks();
        }
    });
}

function initMobileMenu() {
    const toggle = document.querySelector('.menu-toggle');
    const sidebar = document.querySelector('.sidebar');
    if (toggle && sidebar) {
        toggle.addEventListener('click', () => sidebar.classList.toggle('show'));
        document.addEventListener('click', (e) => {
            if (!sidebar.contains(e.target) && !toggle.contains(e.target)) {
                sidebar.classList.remove('show');
            }
        });
    }
}

async function loadInitialData() {
    await Promise.all([loadStatus(), loadConfig(), loadLogFiles(), loadDownloaders(), loadDownloaderConfigs(), loadUsers()]);
    loadTasks();
}

function startAutoRefresh() {
    state.autoRefresh = setInterval(() => {
        if (!dashboardState.isConnected) {
            loadStatus();
        }
        if (document.querySelector('#page-tasks.active')) loadTasks();
        if (!dashboardState.isConnected) {
            if (document.querySelector('#page-dashboard.active')) updateRecentActivity();
        }
    }, 5000);
}

function switchPage(pageName) {
    el.navItems.forEach(item => item.classList.toggle('active', item.dataset.page === pageName));
    el.pages.forEach(page => {
        const isActive = page.id === `page-${pageName}`;
        if (isActive) {
            page.classList.remove('active');
            void page.offsetWidth;
        }
        page.classList.toggle('active', isActive);
    });
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) sidebar.classList.remove('show');
    if (pageName === 'logs') loadLogFiles();
    if (pageName === 'downloaders') loadDownloaderConfigs();
    if (pageName === 'users') loadUsers();
    if (pageName === 'online') initOnlinePage(); else stopOnlinePolling();
}

function switchTaskTab(tabName) {
    state.currentTab = tabName;
    el.taskTabs.forEach(tab => tab.classList.toggle('active', tab.dataset.tab === tabName));
    el.clearFailedBtn.style.display = tabName === 'failed' ? 'inline-flex' : 'none';
    el.retryAllBtn.style.display = tabName === 'failed' ? 'inline-flex' : 'none';
    state.taskPage = 1;
    state.taskSearch = '';
    if (el.taskSearchInput) el.taskSearchInput.value = '';
    updateTaskList();
}

// ===== 状态 =====

async function loadStatus() {
    try {
        state.status = await loadStatusFromApi();
        updateStatusDisplay();
    } catch (e) { if (!e.message.includes('登录已过期')) console.error('加载状态失败:', e); }
}

function updateStatusDisplay() {
    const s = state.status;
    if (!s) return;
    el.statusDot.className = `status-dot ${s.is_running ? 'running' : 'stopped'}`;
    el.statusText.textContent = s.is_running ? '运行中' : '已停止';
    el.queueCount.textContent = s.queue_size;
    el.statQueue.textContent = s.queue_size;
    el.statProcessing.textContent = s.processing_count;
    el.statCompleted.textContent = s.completed_count;
    el.statFailed.textContent = s.failed_count;
}

// ===== 任务 =====

async function loadTasks() {
    try {
        state.tasks = await loadTasksFromApi();
        updateTaskList();
        updateRecentActivity();
    } catch (e) { if (!e.message.includes('登录已过期')) console.error('加载任务失败:', e); }
}

async function updateTaskList() {
    const tab = state.currentTab;
    const isLiveTab = tab === 'queued' || tab === 'processing';

    if (isLiveTab) {
        // 实时标签页（队列中/处理中）：使用 state 数据，无分页
        let files = [];
        let statusBadge = '';
        if (tab === 'queued') {
            files = state.tasks.queued.map(f => ({ path: f }));
            statusBadge = '<span class="badge badge-info">队列中</span>';
        } else {
            files = state.tasks.processing.map(f => ({ path: f }));
            statusBadge = '<span class="badge badge-warning">处理中</span>';
        }
        if (files.length === 0) {
            el.taskList.innerHTML = `<tr><td colspan="4"><div class="empty-state"><p>暂无数据</p></div></td></tr>`;
        } else {
            el.taskList.innerHTML = files.map(file => `<tr>
                <td class="col-path" style="font-family:monospace;font-size:0.8125rem">${escapeHtml(file.path)}</td>
                <td class="col-status">${statusBadge}</td>
                <td class="col-time" style="color:var(--text-muted)">-</td>
                <td class="col-actions"></td>
            </tr>`).join('');
        }
        if (el.taskPagination) el.taskPagination.style.display = 'none';
        return;
    }

    // 已完成/失败：使用分页 API
    try {
        const data = await loadTaskListPaginated(state.taskPage, state.taskPageSize, tab === 'failed' ? 'failed' : 'completed', state.taskSearch);
        const items = data.items || [];
        state.taskTotal = data.total || 0;

        if (items.length === 0) {
            el.taskList.innerHTML = `<tr><td colspan="4"><div class="empty-state"><p>暂无数据</p></div></td></tr>`;
        } else {
            const statusBadge = tab === 'completed'
                ? '<span class="badge badge-success">已完成</span>'
                : '<span class="badge badge-danger">失败</span>';
            el.taskList.innerHTML = items.map(file => `<tr>
                <td class="col-path" style="font-family:monospace;font-size:0.8125rem">${escapeHtml(file.path)}</td>
                <td class="col-status">${statusBadge}${file.error ? `<br><small style="color:var(--accent-danger)">${escapeHtml(file.error)}</small>` : ''}</td>
                <td class="col-time" style="color:var(--text-muted);font-size:0.8125rem">${formatRelativeTime(file.time)}</td>
                <td class="col-actions">${tab === 'failed' ? `<button class="btn btn-primary btn-sm" onclick="retryTask('${escapeHtml(file.path)}')">重试</button>
                    <button class="btn btn-danger btn-sm" onclick="confirmClearFailed('${escapeHtml(file.path)}')">清除</button>` : ''}</td>
            </tr>`).join('');
        }
        updateTaskPagination(data);
    } catch (e) {
        if (!e.message.includes('登录已过期')) {
            el.taskList.innerHTML = `<tr><td colspan="4"><div class="empty-state"><p>加载失败</p></div></td></tr>`;
        }
    }
}

function updateTaskPagination(data) {
    const total = data.total || 0;
    const page = data.page || 1;
    const pageSize = data.page_size || 20;
    const totalPages = Math.ceil(total / pageSize) || 1;
    if (el.taskTotal) el.taskTotal.textContent = total;
    if (el.taskPage) el.taskPage.textContent = page;
    if (el.taskTotalPages) el.taskTotalPages.textContent = totalPages;
    if (el.taskPrevBtn) el.taskPrevBtn.disabled = page <= 1;
    if (el.taskNextBtn) el.taskNextBtn.disabled = page >= totalPages;
    if (el.taskPagination) el.taskPagination.style.display = total > 0 ? 'flex' : 'none';
    state.taskPage = page;
}

function formatRelativeTime(isoTime) {
    if (!isoTime) return '';
    const now = Date.now();
    const then = new Date(isoTime).getTime();
    const diffMs = now - then;
    if (diffMs < 0) return '刚刚';
    const seconds = Math.floor(diffMs / 1000);
    if (seconds < 60) return '刚刚';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}分钟前`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}小时前`;
    const days = Math.floor(hours / 24);
    if (days < 30) return `${days}天前`;
    const months = Math.floor(days / 30);
    if (months < 12) return `${months}个月前`;
    return `${Math.floor(months / 12)}年前`;
}

async function updateRecentActivity() {
    try {
        const data = await loadRecentActivityPaginated(state.recentPage, state.recentPageSize, state.recentSearch);
        const items = data.items || [];
        state.recentTotal = data.total || 0;
        if (items.length === 0) {
            el.recentActivity.innerHTML = `<div class="empty-state"><p>暂无数据</p></div>`;
        } else {
            el.recentActivity.innerHTML = items.map((t, i) => `<div class="activity-item" style="animation-delay:${i * 30}ms">
                <div class="activity-dot ${t.status === 'completed' ? 'activity-dot-success' : 'activity-dot-danger'}"></div>
                <div class="activity-path" title="${escapeHtml(t.path)}">${escapeHtml(t.path)}</div>
                <div class="activity-status ${t.status === 'completed' ? 'activity-status-success' : 'activity-status-danger'}">${t.status === 'completed' ? '已完成' : '失败'}</div>
                <div class="activity-time">${formatRelativeTime(t.time)}</div>
            </div>`).join('');
        }
        updateRecentPagination(data);
    } catch (e) {
        if (!e.message.includes('登录已过期')) {
            el.recentActivity.innerHTML = `<div class="empty-state"><p>加载失败</p></div>`;
        }
    }
}

function updateRecentPagination(data) {
    const total = data.total || 0;
    const page = data.page || 1;
    const pageSize = data.page_size || 20;
    const totalPages = Math.ceil(total / pageSize) || 1;
    if (el.recentTotal) el.recentTotal.textContent = total;
    if (el.recentPage) el.recentPage.textContent = page;
    if (el.recentTotalPages) el.recentTotalPages.textContent = totalPages;
    if (el.recentPrevBtn) el.recentPrevBtn.disabled = page <= 1;
    if (el.recentNextBtn) el.recentNextBtn.disabled = page >= totalPages;
    if (el.recentPagination) el.recentPagination.style.display = total > 0 ? 'flex' : 'none';
    state.recentPage = page;
}

async function retryTask(filePath) {
    try {
        await retryTaskViaApi(filePath);
        showToast('已重试任务', 'success');
        loadTasks();
    } catch (e) { showToast(`重试失败: ${e.message}`, 'error'); }
}

function confirmClearFailed(filePath) {
    showConfirm('清除失败记录', `确定要清除该失败记录吗？`, async () => {
        try {
            await clearFailedTaskViaApi(filePath);
            showToast('已清除失败记录', 'success');
            loadTasks();
        } catch (e) { showToast(`清除失败: ${e.message}`, 'error'); }
    });
}

async function clearAllFailedTasks() {
    try {
        await clearAllFailedViaApi();
        showToast('已清除所有失败记录', 'success');
        loadTasks();
    } catch (e) { showToast(`清除失败: ${e.message}`, 'error'); }
}

async function retryAllFailedTasks() {
    try {
        const result = await retryAllFailedViaApi();
        showToast(`已重试 ${result.retried_count} 个任务`, 'success');
        loadTasks();
    } catch (e) { showToast(`重试失败: ${e.message}`, 'error'); }
}

// ===== 配置 =====

let _configCurrentSection = 'monitoring';
let _dbManualRules = [];
let _dbReleaseGroups = [];
let _dbLlmProviders = [];
let _dbRuntimeConfig = [];

async function loadConfig() {
    try {
        state.config = await loadConfigFromApi();
        syncLogLevelSelect();
        await loadDbConfigs();
        renderConfigEditor();
    setupConfigNav();
    setupConfigSearch();
    updateConfigBadges();
    } catch (e) { if (!e.message.includes('登录已过期')) console.error('加载配置失败:', e); }
}

async function loadDbConfigs() {
    try {
        const [mr, rg, lp, rt] = await Promise.all([
            loadManualRulesFromApi(),
            loadReleaseGroupsFromApi(),
            loadLlmProvidersFromApi(),
            loadRuntimeConfigFromApi(),
        ]);
        _dbManualRules = mr.rules || [];
        _dbReleaseGroups = rg.groups || [];
        _dbLlmProviders = lp.providers || [];
        _dbRuntimeConfig = rt.configs || [];
        updateConfigBadges();
    } catch (e) { console.error('加载数据库配置失败:', e); }
}

function updateConfigBadges() {
    const mr = document.getElementById('mrCount');
    const rg = document.getElementById('rgCount');
    const lp = document.getElementById('lpCount');
    const rt = document.getElementById('rtCount');
    if (mr) mr.textContent = _dbManualRules.length;
    if (rg) rg.textContent = _dbReleaseGroups.length;
    if (lp) lp.textContent = _dbLlmProviders.length;
    if (rt) rt.textContent = _dbRuntimeConfig.length;
}

function setupConfigNav() {
    document.querySelectorAll('.cnav-item').forEach(item => {
        item.addEventListener('click', () => {
            document.querySelectorAll('.cnav-item').forEach(n => n.classList.remove('active'));
            item.classList.add('active');
            _configCurrentSection = item.dataset.section;
            renderConfigEditor();
            if (_configCurrentSection === '__downloaders') loadDownloaderConfigs();
        });
    });
}

function setupConfigSearch() {
    const input = document.getElementById('configSearchInput');
    if (!input) return;
    input.addEventListener('input', () => {
        const q = input.value.toLowerCase().trim();
        if (!q) {
            document.querySelectorAll('.config-field').forEach(f => f.style.display = '');
            return;
        }
        document.querySelectorAll('.config-field').forEach(f => {
            const label = f.querySelector('.config-field-label')?.textContent?.toLowerCase() || '';
            f.style.display = label.includes(q) ? '' : 'none';
        });
    });
}

function renderConfigEditor() {
    const section = _configCurrentSection;
    if (section.startsWith('__')) {
        renderDbConfigEditor(section);
        return;
    }
    renderIniConfigEditor(section);
}

function renderIniConfigEditor(section) {
    const config = state.config;
    if (!config || !config[section]) {
        el.configEditor.innerHTML = '<div class="config-empty"><span class="empty-icon">⌀</span><span>该配置节无数据</span></div>';
        return;
    }

    let html = `<div class="config-section"><div class="config-section-title">${getSectionLabel(section)}</div><div class="config-grid">`;
    for (const [k, v] of Object.entries(config[section])) {
        if (isFieldHidden(section, k)) continue;
        html += renderConfigField(section, k, v);
    }
    html += `</div>`;
    if (section === 'logging') {
        html += `<div class="config-hint" style="margin-top:12px;font-size:0.8125rem;color:var(--text-muted)">日志文件由系统自动维护，在「日志查看」页面可以直接查看，无需填写其他项。</div>`;
    }
    html += `</div>`;
    el.configEditor.innerHTML = html;

    document.querySelectorAll('#config-editor .toggle-track').forEach(track => {
        track.addEventListener('click', () => {
            const cb = document.getElementById(track.dataset.for);
            if (!cb) return;
            cb.checked = !cb.checked;
            track.classList.toggle('on', cb.checked);
            const label = document.getElementById(`${cb.id}-label`);
            if (label) label.textContent = cb.checked ? '是' : '否';
        });
    });
}

// ===== 配置项中文标签 =====
// 各配置节的中文名称
const SECTION_LABELS = {
    monitoring: '监控配置', tmdb: 'TMDB 配置', naming: '命名规则',
    processing: '处理配置', logging: '日志配置', emos: 'Emos 云盘',
    online_upload: '在线识别上传', telegram: 'Telegram 通知与机器人',
    guessit: 'GuessIt 解析', llm_fallback: 'LLM 兜底识别',
    manual_rules: '手动规则', auth: '登录认证', downloaders: '下载器列表',
    __downloaders: '下载器配置',
};

// 各配置项的中文含义
const FIELD_LABELS = {
    monitoring: {
        watch_dir: '监控目录', output_dir: '输出目录', poll_interval: '轮询间隔（秒）',
        supported_extensions: '支持的扩展名', use_polling: '启用轮询模式',
        polling_interval: '轮询扫描间隔（秒）', path_mappings: '路径映射',
        enable_directory_monitor: '启用目录监控', directory_watch_dir: '目录监控路径',
        directory_output_dir: '目录监控输出目录', directory_organize_mode: '目录整理方式',
        directory_scrape_metadata: '抓取元数据', directory_metadata_format: '元数据文件格式',
        directory_polling_interval: '目录扫描间隔（秒）',
    },
    emos: {
        auth_token: 'Emos 认证令牌', base_url: 'Emos 服务地址',
        file_storage: '默认存储类型', file_storages: '可选存储类型',
        chunk_size_mb: '分片大小（MB）', timeout: '请求超时（秒）',
    },
    online_upload: {
        video_root: '视频根目录', probe_enabled: '上传前用 ffprobe 校验',
        ffprobe_path: 'ffprobe 路径', path_type: '内部入库 path_type',
    },
    naming: {
        tv_show_format: '电视剧命名模板', movie_format: '电影命名模板',
        anime_format: '动漫命名模板', simple_format: '简单命名模板',
    },
    tmdb: {
        api_key: 'TMDB API 密钥', language: '语言', region: '地区',
        retry_count: '重试次数', timeout: '超时（秒）', base_url: 'API 地址',
    },
    processing: {
        rename_only: '仅重命名（不上传）', copy_mode: '复制模式',
        delete_original: '删除原始文件', delete_after_upload: '上传后删除源文件',
        min_file_size: '最小文件大小', ignore_patterns: '忽略规则',
        upload_targets: '上传目标', max_upload_workers: '最大并发上传数',
    },
    logging: {
        log_level: '日志等级',
    },
    telegram: {
        bot_token: '机器人 Token', chat_id: '会话 ID', enabled: '启用通知',
        reply_enabled: '启用机器人回复修正', allowed_user_ids: '允许的用户 ID',
        poll_timeout: '长轮询超时（秒）', channel_chat_id: '频道 ID',
    },
    llm_fallback: { enabled: '启用 LLM 兜底识别', max_concurrent: '最大并发数' },
    llm_provider_1: { name: '名称', api_url: '接口地址', api_key: '密钥', model: '模型', enabled: '启用', weight: '权重', timeout: '超时（秒）', max_retries: '最大重试次数' },
    llm_provider_2: { name: '名称', api_url: '接口地址', api_key: '密钥', model: '模型', enabled: '启用', weight: '权重', timeout: '超时（秒）', max_retries: '最大重试次数' },
    llm_provider_3: { name: '名称', api_url: '接口地址', api_key: '密钥', model: '模型', enabled: '启用', weight: '权重', timeout: '超时（秒）', max_retries: '最大重试次数' },
    guessit: { enabled: '启用 GuessIt 增强识别', prefer_guessit: '优先使用 GuessIt 结果' },
    manual_rules: { enabled: '启用手动规则', normalize_symbols: '归一化规则符号', rules: '规则列表' },
    auth: { enabled: '启用登录认证', username: '用户名', password: '密码' },
    downloader: {
        type: '下载器类型', name: '显示名称', host: '主机地址', port: '端口',
        rpc_url: 'RPC 地址', secret: 'RPC 密钥', password: '密码', username: '用户名',
        monitor_mode: '监控模式', path_mappings: '路径映射',
        websocket_reconnect_delay: '断线重连延迟（秒）', enabled: '启用',
    },
};

// 日志配置只保留「日志等级」，其余项由系统自动维护
const HIDDEN_FIELDS = { logging: ['log_file', 'console_log', 'file_log', 'log_max_bytes', 'log_backup_count'] };

// 需要下拉选择的配置项
const FIELD_OPTIONS = {
    'logging.log_level': ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
    'monitoring.directory_organize_mode': ['copy', 'move'],
    'monitoring.directory_metadata_format': ['nfo', 'json', 'both'],
    'emos.file_storage': ['internal', 'global', 'default', 'google_drive', 'zn_r2_upload'],
    'downloader.type': ['aria2', 'qbittorrent'],
    'downloader.monitor_mode': ['polling', 'websocket', 'webhook'],
};

function normalizeFieldSection(section) {
    return section.startsWith('downloader.') ? 'downloader' : section;
}

function getFieldLabel(section, key) {
    const group = FIELD_LABELS[normalizeFieldSection(section)] || {};
    return group[key] || key;
}

function getFieldOptions(section, key) {
    return FIELD_OPTIONS[`${section}.${key}`] || FIELD_OPTIONS[`${normalizeFieldSection(section)}.${key}`] || null;
}

function isFieldHidden(section, key) {
    const hidden = HIDDEN_FIELDS[section] || [];
    return hidden.includes(key);
}

function getSectionLabel(section) {
    if (SECTION_LABELS[section]) return SECTION_LABELS[section];
    if (section.startsWith('downloader.')) return '下载器 · ' + section.slice('downloader.'.length);
    return section;
}

function renderConfigField(section, key, value) {
    const inputId = `cfg-${section}-${key}`;
    const inputName = `${section}.${key}`;
    const label = getFieldLabel(section, key);
    const options = getFieldOptions(section, key);
    const isSecret = ['password','token','secret','api_key','apikey'].some(s => key.toLowerCase().includes(s));

    let inputHtml;
    if (options) {
        const current = String(value);
        inputHtml = `<select id="${inputId}" name="${inputName}">` + options.map(opt =>
            `<option value="${escapeHtml(opt)}" ${opt === current ? 'selected' : ''}>${escapeHtml(opt)}</option>`).join('') + `</select>`;
    } else if (typeof value === 'boolean') {
        inputHtml = `<div class="toggle-wrap">
            <div class="toggle-track ${value ? 'on' : ''}" data-for="${inputId}">
                <div class="toggle-thumb"></div>
            </div>
            <input type="checkbox" id="${inputId}" name="${inputName}" ${value ? 'checked' : ''} style="display:none">
            <span class="toggle-label" id="${inputId}-label">${value ? '启用' : '禁用'}</span>
        </div>`;
    } else if (Array.isArray(value)) {
        inputHtml = `<input type="text" id="${inputId}" name="${inputName}" value="${escapeHtml(value.join(', '))}">`;
    } else if (typeof value === 'object' && value !== null) {
        inputHtml = `<textarea id="${inputId}" name="${inputName}">${escapeHtml(JSON.stringify(value, null, 2))}</textarea>`;
    } else if (isSecret) {
        inputHtml = `<input type="password" id="${inputId}" name="${inputName}" value="${escapeHtml(String(value))}" spellcheck="false">`;
    } else {
        inputHtml = `<input type="text" id="${inputId}" name="${inputName}" value="${escapeHtml(String(value))}" spellcheck="false">`;
    }

    return `<div class="config-field">
        <div class="config-field-label" title="${escapeHtml(inputName)}"><span>${escapeHtml(label)}</span> <code>${escapeHtml(section)}.${escapeHtml(key)}</code></div>
        ${inputHtml}
    </div>`;
}

function renderDbConfigEditor(section) {
    switch (section) {
        case '__manual_rules': return renderManualRules();
        case '__release_groups': return renderReleaseGroups();
        case '__llm_providers': return renderLlmProviders();
        case '__runtime': return renderRuntimeConfig();
        case '__downloaders': return renderDownloaderManager();
    }
}

// ===== 手动规则 =====

function renderManualRules() {
    const rules = _dbManualRules;
    let html = `<div class="config-section"><div class="config-section-title">手动规则</div>`;
    if (rules.length === 0) {
        html += `<div class="config-empty"><span class="empty-icon">⌀</span><span>暂无规则</span></div>`;
    } else {
        html += `<div class="config-table-wrap"><table class="config-table">
            <thead><tr><th style="width:36px">#</th><th>规则内容</th><th style="width:60px">启用</th><th style="width:140px">操作</th></tr></thead><tbody>`;
        rules.forEach((r, i) => {
            html += `<tr>
                <td style="color:var(--text-muted);font-size:0.75rem">${i + 1}</td>
                <td><input type="text" class="mr-text" data-id="${r.id}" value="${escapeHtml(r.rule_text)}" style="font-family:monospace"></td>
                <td><input type="checkbox" class="mr-enabled" data-id="${r.id}" ${r.enabled ? 'checked' : ''} style="width:auto"></td>
                <td><div class="btn-cell">
                    <button class="save-btn" onclick="saveManualRule(${r.id})">保存</button>
                    <button class="del-btn" onclick="deleteManualRule(${r.id})">删除</button>
                </div></td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }
    html += `<div class="add-row-bar">
        <input type="text" id="newMrText" placeholder="新规则内容..." style="flex:1;font-family:monospace">
        <button class="add-btn" onclick="addManualRule()">+ 添加</button>
    </div></div>`;
    el.configEditor.innerHTML = html;
}

async function addManualRule() {
    const input = document.getElementById('newMrText');
    const text = input?.value.trim();
    if (!text) return showToast('请输入规则内容', 'warning');
    try {
        await createManualRuleViaApi({ rule_text: text, enabled: true, sort_order: _dbManualRules.length });
        showToast('规则已添加', 'success');
        await loadDbConfigs();
        renderManualRules();
    } catch (e) { showToast(`添加失败: ${e.message}`, 'error'); }
}

window.addManualRule = addManualRule;

async function saveManualRule(id) {
    const textInput = document.querySelector(`.mr-text[data-id="${id}"]`);
    const enabledInput = document.querySelector(`.mr-enabled[data-id="${id}"]`);
    if (!textInput) return;
    try {
        await updateManualRuleViaApi(id, {
            rule_text: textInput.value,
            enabled: enabledInput?.checked ?? true,
            sort_order: 0,
        });
        showToast('规则已保存', 'success');
        await loadDbConfigs();
        renderManualRules();
    } catch (e) { showToast(`保存失败: ${e.message}`, 'error'); }
}

window.saveManualRule = saveManualRule;

async function deleteManualRule(id) {
    if (!confirm('确定删除此规则？')) return;
    try {
        await deleteManualRuleViaApi(id);
        showToast('规则已删除', 'success');
        await loadDbConfigs();
        renderManualRules();
    } catch (e) { showToast(`删除失败: ${e.message}`, 'error'); }
}

window.deleteManualRule = deleteManualRule;

// ===== 字幕组映射 =====

function renderReleaseGroups() {
    const groups = _dbReleaseGroups;
    let html = `<div class="config-section"><div class="config-section-title">字幕组映射</div>`;
    if (groups.length === 0) {
        html += `<div class="config-empty"><span class="empty-icon">⌀</span><span>暂无映射</span></div>`;
    } else {
        html += `<div class="config-table-wrap"><table class="config-table">
            <thead><tr><th>字幕组名称</th><th>类型</th><th style="width:140px">操作</th></tr></thead><tbody>`;
        groups.forEach(g => {
            html += `<tr>
                <td><input type="text" class="rg-name" data-id="${g.id}" value="${escapeHtml(g.group_name)}"></td>
                <td><select class="rg-type" data-id="${g.id}">
                    <option value="anime" ${g.content_type === 'anime' ? 'selected' : ''}>动画</option>
                    <option value="drama" ${g.content_type === 'drama' ? 'selected' : ''}>剧集</option>
                    <option value="movie" ${g.content_type === 'movie' ? 'selected' : ''}>电影</option>
                </select></td>
                <td><div class="btn-cell">
                    <button class="save-btn" onclick="saveReleaseGroup(${g.id})">保存</button>
                    <button class="del-btn" onclick="deleteReleaseGroup(${g.id})">删除</button>
                </div></td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }
    html += `<div class="add-row-bar">
        <input type="text" id="newRgName" placeholder="字幕组名称..." style="flex:1">
        <select id="newRgType">
            <option value="anime">动画</option>
            <option value="drama">剧集</option>
            <option value="movie">电影</option>
        </select>
        <button class="add-btn" onclick="addReleaseGroup()">+ 添加</button>
    </div></div>`;
    el.configEditor.innerHTML = html;
}

async function addReleaseGroup() {
    const name = document.getElementById('newRgName')?.value.trim();
    const type = document.getElementById('newRgType')?.value;
    if (!name) return showToast('请输入字幕组名称', 'warning');
    try {
        await createReleaseGroupViaApi({ group_name: name, content_type: type });
        showToast('映射已添加', 'success');
        await loadDbConfigs();
        renderReleaseGroups();
    } catch (e) { showToast(`添加失败: ${e.message}`, 'error'); }
}

window.addReleaseGroup = addReleaseGroup;

async function saveReleaseGroup(id) {
    const nameInput = document.querySelector(`.rg-name[data-id="${id}"]`);
    const typeSelect = document.querySelector(`.rg-type[data-id="${id}"]`);
    if (!nameInput) return;
    try {
        await updateReleaseGroupViaApi(id, {
            group_name: nameInput.value,
            content_type: typeSelect.value,
        });
        showToast('映射已保存', 'success');
        await loadDbConfigs();
        renderReleaseGroups();
    } catch (e) { showToast(`保存失败: ${e.message}`, 'error'); }
}

window.saveReleaseGroup = saveReleaseGroup;

async function deleteReleaseGroup(id) {
    if (!confirm('确定删除此映射？')) return;
    try {
        await deleteReleaseGroupViaApi(id);
        showToast('映射已删除', 'success');
        await loadDbConfigs();
        renderReleaseGroups();
    } catch (e) { showToast(`删除失败: ${e.message}`, 'error'); }
}

window.deleteReleaseGroup = deleteReleaseGroup;

// ===== LLM 提供商 =====

function renderLlmProviders() {
    const providers = _dbLlmProviders;
    let html = `<div class="config-section"><div class="config-section-title">LLM 提供商</div>`;
    if (providers.length === 0) {
        html += `<div class="config-empty"><span class="empty-icon">⌀</span><span>暂无提供商</span></div>`;
    } else {
        html += `<div class="config-table-wrap"><table class="config-table">
            <thead><tr><th class="col-name">名称</th><th class="col-url">API URL</th><th class="col-key">API Key</th><th class="col-model">模型</th><th class="col-num">权重</th><th class="col-num">超时</th><th class="col-check">启用</th><th class="col-actions">操作</th></tr></thead><tbody>`;
        providers.forEach(p => {
            const keyPlaceholder = p.has_key ? '已设置，留空不变' : '未设置';
            html += `<tr>
                <td><input class="lp-name" data-id="${p.id}" value="${escapeHtml(p.name)}" style="width:85px"></td>
                <td><input class="lp-url" data-id="${p.id}" value="${escapeHtml(p.api_url)}" style="font-family:monospace"></td>
                <td><input class="lp-apikey" data-id="${p.id}" value="" placeholder="${keyPlaceholder}" type="password"></td>
                <td><input class="lp-model" data-id="${p.id}" value="${escapeHtml(p.model || '')}" style="width:85px"></td>
                <td><input class="lp-weight" data-id="${p.id}" value="${p.weight}" type="number" min="0"></td>
                <td><input class="lp-timeout" data-id="${p.id}" value="${p.timeout}" type="number" min="1"></td>
                <td class="col-check"><input class="lp-enabled" data-id="${p.id}" type="checkbox" ${p.enabled ? 'checked' : ''}></td>
                <td><div class="btn-cell">
                    <button class="save-btn" onclick="saveLlmProvider(${p.id})">保存</button>
                    <button class="del-btn" onclick="deleteLlmProvider(${p.id})">删除</button>
                </div></td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }
    html += `<div class="add-row-bar">
        <input type="text" id="newLpName" placeholder="名称..." style="width:90px">
        <input type="text" id="newLpUrl" placeholder="API URL..." style="font-family:monospace">
        <input type="password" id="newLpApiKey" placeholder="API Key...">
        <input type="text" id="newLpModel" placeholder="模型..." style="width:90px">
        <button class="add-btn" onclick="addLlmProvider()">+ 添加</button>
    </div></div>`;
    el.configEditor.innerHTML = html;
}

async function addLlmProvider() {
    const name = document.getElementById('newLpName')?.value.trim();
    const url = document.getElementById('newLpUrl')?.value.trim();
    const key = document.getElementById('newLpApiKey')?.value.trim();
    if (!name || !url) return showToast('请填写名称和 API URL', 'warning');
    try {
        await createLlmProviderViaApi({ name, api_url: url, api_key: key || '', model: document.getElementById('newLpModel')?.value.trim() || '' });
        showToast('提供商已添加', 'success');
        await loadDbConfigs();
        renderLlmProviders();
    } catch (e) { showToast(`添加失败: ${e.message}`, 'error'); }
}

window.addLlmProvider = addLlmProvider;

async function saveLlmProvider(id) {
    const g = (cls) => document.querySelector(`.${cls}[data-id="${id}"]`);
    try {
        await updateLlmProviderViaApi(id, {
            name: g('lp-name')?.value || '',
            api_url: g('lp-url')?.value || '',
            api_key: g('lp-apikey')?.value || '',
            model: g('lp-model')?.value || '',
            enabled: g('lp-enabled')?.checked ?? true,
            weight: parseInt(g('lp-weight')?.value) || 1,
            timeout: parseInt(g('lp-timeout')?.value) || 30,
            max_retries: 2,
        });
        showToast('提供商已保存', 'success');
        await loadDbConfigs();
        renderLlmProviders();
    } catch (e) { showToast(`保存失败: ${e.message}`, 'error'); }
}

window.saveLlmProvider = saveLlmProvider;

async function deleteLlmProvider(id) {
    if (!confirm('确定删除此提供商？')) return;
    try {
        await deleteLlmProviderViaApi(id);
        showToast('提供商已删除', 'success');
        await loadDbConfigs();
        renderLlmProviders();
    } catch (e) { showToast(`删除失败: ${e.message}`, 'error'); }
}

window.deleteLlmProvider = deleteLlmProvider;

// ===== 运行时配置 =====

function renderRuntimeConfig() {
    const configs = _dbRuntimeConfig;
    let html = `<div class="config-section"><div class="config-section-title">运行时配置</div>`;
    if (configs.length === 0) {
        html += `<div class="config-empty"><span class="empty-icon">⌀</span><span>暂无配置项</span></div>`;
    } else {
        html += `<div class="config-table-wrap"><table class="config-table">
            <thead><tr><th>配置键</th><th>值</th><th>说明</th><th style="width:90px">操作</th></tr></thead><tbody>`;
        configs.forEach(c => {
            html += `<tr>
                <td style="font-family:monospace;font-size:0.75rem;color:var(--text-muted)">${escapeHtml(c.key)}</td>
                <td><input class="rt-value" data-key="${escapeHtml(c.key)}" value="${escapeHtml(c.value || '')}" style="font-family:monospace"></td>
                <td style="font-size:0.75rem;color:var(--text-muted)">${escapeHtml(c.description || '')}</td>
                <td><div class="btn-cell"><button class="save-btn" onclick="saveRuntimeConfig('${escapeHtml(c.key)}')">保存</button></div></td>
            </tr>`;
        });
        html += `</tbody></table></div>`;
    }
    html += `</div>`;
    el.configEditor.innerHTML = html;
}

async function saveRuntimeConfig(key) {
    const input = document.querySelector(`.rt-value[data-key="${CSS.escape(key)}"]`);
    if (!input) return;
    try {
        await updateRuntimeConfigViaApi(key, { value: input.value });
        showToast('配置已保存', 'success');
        await loadDbConfigs();
        renderRuntimeConfig();
    } catch (e) { showToast(`保存失败: ${e.message}`, 'error'); }
}

window.saveRuntimeConfig = saveRuntimeConfig;

// ===== 保存 / 重新加载 INI 配置 =====

async function reloadConfig() {
    try {
        await reloadConfigViaApi();
        await loadConfig();
        showToast('配置已重新加载', 'success');
    } catch (e) { showToast(`重新加载失败: ${e.message}`, 'error'); }
}

async function saveConfig() {
    const inputs = el.configEditor.querySelectorAll('[name]');
    const sections = {};

    inputs.forEach(input => {
        const [section, ...keyParts] = input.name.split('.');
        const key = keyParts.join('.');
        if (!sections[section]) sections[section] = {};
        let value = input.value;
        if (input.type === 'checkbox') {
            value = input.checked;
        } else if (!isNaN(Number(value)) && value.trim() !== '') {
            const num = Number(value);
            if (Number.isFinite(num)) value = num;
        }
        sections[section][key] = value;
    });

    let successCount = 0;
    let failCount = 0;
    for (const [section, data] of Object.entries(sections)) {
        try {
            const current = state.config[section] || {};
            const changed = {};
            for (const k of Object.keys(data)) {
                if (JSON.stringify(data[k]) !== JSON.stringify(current[k])) {
                    changed[k] = data[k];
                }
            }
            if (Object.keys(changed).length === 0) { successCount++; continue; }
            await saveConfigToApi(section, changed);
            await loadConfig();
            successCount++;
        } catch (e) {
            console.error(`保存配置节 ${section} 失败:`, e);
            failCount++;
        }
    }

    if (failCount === 0) {
        showToast(`配置已保存（${successCount} 节）`, 'success');
    } else {
        showToast(`保存完成: ${successCount} 节成功, ${failCount} 节失败`, 'warning');
    }
}

// ===== 日志 =====

async function loadLogFiles() {
    try {
        const result = await loadLogFilesFromApi();
        const files = result.files || [];
        // 自动选择最新日志文件，无需手动选择
        state.currentLogFile = result.current || files[0] || null;
        syncLogLevelSelect();
        if (state.currentLogFile) {
            await loadLogContent();
        } else {
            el.logViewer.innerHTML = '<div class="log-line" style="color:var(--text-muted)">暂无日志文件，稍后点击「刷新」重试</div>';
        }
    } catch (e) { if (!e.message.includes('登录已过期')) console.error('加载日志文件列表失败:', e); }
}

function syncLogLevelSelect() {
    if (!el.logLevelSelect) return;
    const level = (state.config && state.config.logging && state.config.logging.log_level) || 'INFO';
    el.logLevelSelect.value = String(level).toUpperCase();
}

async function changeLogLevel() {
    if (!el.logLevelSelect) return;
    const level = el.logLevelSelect.value;
    try {
        await saveConfigToApi('logging', { log_level: level });
        await loadConfig();
        showToast(`日志等级已切换为 ${level}（立即生效）`, 'success');
    } catch (e) { showToast(`设置日志等级失败: ${e.message}`, 'error'); }
}

async function loadLogContent() {
    const filename = state.currentLogFile;
    if (!filename) { await loadLogFiles(); return; }
    try {
        const text = await loadLogContentFromApi(filename);
        el.logViewer.innerHTML = text.split('\n').map(line =>
            `<div class="${classifyLogLine(line)}">${escapeHtml(line)}</div>`
        ).join('');
        if (el.autoScrollCheck.checked) el.logViewer.scrollTop = el.logViewer.scrollHeight;
    } catch (e) { showToast(`加载日志失败: ${e.message}`, 'error'); }
}

function toggleLiveLog() {
    const filename = state.currentLogFile;
    if (!filename) { el.liveLogCheck.checked = false; showToast('暂无日志文件', 'warning'); return; }
    if (el.liveLogCheck.checked) startLiveLog(filename);
    else stopLiveLog();
}

function startLiveLog(filename) {
    const token = getAuthToken();
    const wsUrl = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/logs/ws/${encodeURIComponent(filename)}`;
    state.logWebSocket = new WebSocket(wsUrl);
    state.logWebSocket.onopen = () => {
        if (token) state.logWebSocket.send(JSON.stringify({ type: 'auth', token }));
    };
    state.logWebSocket.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'log') {
            el.logViewer.innerHTML += `<div class="${classifyLogLine(data.content)}">${escapeHtml(data.content)}</div>`;
            if (el.autoScrollCheck.checked) el.logViewer.scrollTop = el.logViewer.scrollHeight;
        }
    };
    state.logWebSocket.onerror = () => {
        showToast('WebSocket 连接失败', 'error');
        el.liveLogCheck.checked = false;
    };
    state.logWebSocket.onclose = () => { state.logWebSocket = null; };
}

function stopLiveLog() {
    if (state.logWebSocket) { state.logWebSocket.close(); state.logWebSocket = null; }
}

// ===== 手动处理 =====

async function browseFile() {
    try {
        const result = await browsePathFromApi(el.manualFilePath.value);
        showBrowseModal(result);
    } catch (e) { showToast(`浏览失败: ${e.message}`, 'error'); }
}

function showBrowseModal(data) {
    const titleEl = document.getElementById('modalTitle');
    if (titleEl) titleEl.textContent = '选择文件';

    let html = `<div class="file-browser"><div class="file-browser-header">
        ${data.parent ? `<button class="btn btn-secondary btn-sm" data-path="${escapeHtml(data.parent)}" data-type="parent">..</button>` : ''}
        <div class="file-browser-path">${escapeHtml(data.path) || '根目录'}</div>
    </div><div class="file-list">`;

    (data.directories || []).forEach(dir => {
        const fullPath = data.path ? `${data.path}\\${dir}` : dir;
        html += `<div class="file-item" data-path="${escapeHtml(fullPath)}" data-type="dir">
            <svg class="file-icon folder" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4H4a2 2 0 00-2 2v12a2 2 0 002 2h16a2 2 0 002-2V8a2 2 0 00-2-2h-8l-2-2z"/></svg>
            <span>${escapeHtml(dir)}</span>
        </div>`;
    });

    (data.files || []).forEach(file => {
        const isVideo = ['.mp4','.mkv','.avi','.mov','.wmv','.flv'].includes(file.extension);
        const fullPath = data.path ? `${data.path}\\${file.name}` : file.name;
        html += `<div class="file-item" data-path="${escapeHtml(fullPath)}" data-type="file">
            <svg class="file-icon ${isVideo ? 'video' : ''}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/>
            </svg>
            <span>${escapeHtml(file.name)}</span>
            <small style="color:var(--text-muted);margin-left:auto">${formatSize(file.size)}</small>
        </div>`;
    });

    html += '</div></div>';
    const body = document.getElementById('modalBody');
    if (body) {
        body.innerHTML = html;
        const fileList = body.querySelector('.file-list');
        if (fileList) {
            fileList.addEventListener('click', onFileListClick);
        }
        const parentBtn = body.querySelector('[data-type="parent"]');
        if (parentBtn) {
            parentBtn.addEventListener('click', () => browsePath(parentBtn.dataset.path));
        }
    }
    showModal(false);
}

function onFileListClick(e) {
    const item = e.target.closest('.file-item');
    if (!item || !item.dataset.path) return;
    const path = item.dataset.path;
    if (item.dataset.type === 'dir') {
        browsePath(path);
    } else {
        selectFile(path);
    }
}

async function browsePath(path) {
    try {
        const result = await browsePathFromApi(path);
        showBrowseModal(result);
    } catch (e) { showToast(`浏览失败: ${e.message}`, 'error'); }
}

function selectFile(path) {
    el.manualFilePath.value = path;
    hideModal();
}

function addToFileList() {
    const path = el.manualFilePath.value.trim();
    if (!path) { showToast('请输入文件路径', 'warning'); return; }
    if (state.selectedFiles.some(f => f.path === path)) {
        showToast('文件已存在列表中', 'warning');
        return;
    }
    state.selectedFiles.push({ path, checked: true });
    renderFileList();
    showToast('已添加到列表', 'success');
}

function removeFromFileList(index) {
    state.selectedFiles.splice(index, 1);
    renderFileList();
}

function renderFileList() {
    if (state.selectedFiles.length === 0) {
        el.fileListPanel.style.display = 'none';
        return;
    }
    el.fileListPanel.style.display = 'block';
    el.selectedFilesList.innerHTML = state.selectedFiles.map((f, i) =>
        `<tr>
            <td><input type="checkbox" class="file-checkbox" data-index="${i}" ${f.checked ? 'checked' : ''}></td>
            <td style="font-family:var(--font-mono);font-size:13px">${escapeHtml(f.path)}</td>
            <td><button class="btn btn-danger btn-sm" onclick="removeFromFileList(${i})">移除</button></td>
        </tr>`
    ).join('');
    el.fileListCount.textContent = `共 ${state.selectedFiles.length} 个文件`;

    el.selectedFilesList.querySelectorAll('.file-checkbox').forEach(cb => {
        cb.addEventListener('change', () => {
            const idx = parseInt(cb.dataset.index);
            if (!isNaN(idx) && state.selectedFiles[idx]) {
                state.selectedFiles[idx].checked = cb.checked;
            }
        });
    });
}

async function previewFile() {
    const filePath = el.manualFilePath.value.trim();
    if (!filePath) { showToast('请输入文件路径', 'warning'); return; }
    setButtonLoading(el.previewFileBtn, true);
    try {
        const result = await previewFileViaApi(filePath);
        if (result.success) {
            el.previewResult.style.display = 'block';
            el.previewContent.innerHTML = `
                <div class="form-group"><label class="form-label">原始名称</label>
                    <div style="font-family:monospace">${escapeHtml(result.original_name)}</div></div>
                <div class="form-group"><label class="form-label">建议名称</label>
                    <div style="font-family:monospace;color:var(--accent-primary)">${escapeHtml(result.suggested_name || '无法生成')}</div></div>
                <div class="form-group"><label class="form-label">媒体类型</label>
                    <span class="badge badge-info">${result.media_type || '未知'}</span></div>
                <div class="form-group"><label class="form-label">元数据</label>
                    <pre style="background:var(--bg-primary);padding:12px;border-radius:8px;overflow-x:auto">${JSON.stringify(result.metadata, null, 2)}</pre></div>`;
        } else {
            el.previewResult.style.display = 'block';
            el.previewContent.innerHTML = `<div style="color:var(--accent-danger)">${escapeHtml(result.error)}</div>`;
        }
    } catch (e) { showToast(`预览失败: ${e.message}`, 'error'); }
    finally { setButtonLoading(el.previewFileBtn, false); }
}

async function validateScrape() {
    const filePath = el.manualFilePath.value.trim();
    if (!filePath) { showToast('请输入文件路径', 'warning'); return; }
    setButtonLoading(el.validateScrapeBtn, true);
    el.validateResult.style.display = 'none';
    try {
        const result = await validateScrapeViaApi(filePath);
        el.validateResult.style.display = 'block';
        if (result.success) {
            const icon = result.tmdb_matched ? '✅' : '❌';
            const typeLabel = { tv: '电视剧', movie: '电影' }[result.media_type] || result.media_type || '未知';
            el.validateContent.innerHTML = `
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                    <div class="form-group"><label class="form-label">原始文件名</label>
                        <div style="font-family:monospace;word-break:break-all">${escapeHtml(result.original_name)}</div></div>
                    <div class="form-group"><label class="form-label">识别标题</label>
                        <div style="font-size:16px;font-weight:600">${icon} ${escapeHtml(result.title || '未能识别')}</div></div>
                    <div class="form-group"><label class="form-label">年份</label>
                        <div>${result.year || '-'}</div></div>
                    <div class="form-group"><label class="form-label">媒体类型</label>
                        <span class="badge badge-info">${typeLabel}</span></div>
                    ${result.season ? `<div class="form-group"><label class="form-label">季</label><div>${result.season}</div></div>` : ''}
                    ${result.episode ? `<div class="form-group"><label class="form-label">集</label><div>${result.episode}</div></div>` : ''}
                    ${result.episode_title ? `<div class="form-group" style="grid-column:1/-1"><label class="form-label">集标题</label><div>${escapeHtml(result.episode_title)}</div></div>` : ''}
                    ${result.quality_tags ? `<div class="form-group"><label class="form-label">质量标签</label><div>${escapeHtml(result.quality_tags)}</div></div>` : ''}
                    ${result.release_group ? `<div class="form-group"><label class="form-label">发布组</label><div>${escapeHtml(result.release_group)}</div></div>` : ''}
                    <div class="form-group"><label class="form-label">TMDB刮削</label>
                        <span class="badge ${result.tmdb_matched ? 'badge-success' : 'badge-danger'}">${result.tmdb_matched ? '成功' : '失败'}</span></div>
                    ${result.confidence ? `<div class="form-group"><label class="form-label">匹配分数</label><div>${(result.confidence * 100).toFixed(1)}%</div></div>` : ''}
                    ${result.suggested_name ? `<div class="form-group" style="grid-column:1/-1">
                        <label class="form-label">建议命名</label>
                        <div style="font-family:monospace;color:var(--accent-primary);word-break:break-all">${escapeHtml(result.suggested_name)}</div></div>` : ''}
                    ${result.suggested_path ? `<div class="form-group" style="grid-column:1/-1">
                        <label class="form-label">建议路径</label>
                        <div style="font-family:monospace;color:var(--text-muted);font-size:13px;word-break:break-all">${escapeHtml(result.suggested_path)}</div></div>` : ''}
                </div>
                ${result.tmdb_info ? `
                <div style="margin-top:16px;padding:12px;background:var(--bg-primary);border-radius:8px">
                    <div style="font-weight:600;margin-bottom:8px">TMDB 匹配详情</div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">
                        <div><label style="color:var(--text-muted)">ID</label><div>${result.tmdb_info.id}</div></div>
                        <div><label style="color:var(--text-muted)">原始标题</label><div>${escapeHtml(result.tmdb_info.original_title || '-')}</div></div>
                        <div style="grid-column:1/-1"><label style="color:var(--text-muted)">简介</label>
                            <div>${escapeHtml(result.tmdb_info.overview || '暂无')}</div></div>
                    </div>
                </div>` : ''}`;
        } else {
            el.validateContent.innerHTML = `<div style="color:var(--accent-danger)">${escapeHtml(result.error)}</div>`;
        }
    } catch (e) { showToast(`验证失败: ${e.message}`, 'error'); }
    finally { setButtonLoading(el.validateScrapeBtn, false); }
}

async function scanDirToFileList() {
    const dirPath = el.manualFilePath.value.trim();
    if (!dirPath) { showToast('请输入目录路径', 'warning'); return; }
    setButtonLoading(el.scanDirBtn, true);
    try {
        const result = await scanDirectoryViaApi(dirPath, el.recursiveScanCheck.checked);
        if (!result.files || result.files.length === 0) {
            showToast('未找到视频文件', 'warning');
            return;
        }
        const newFiles = result.files.map(f => ({ path: f, checked: true }));
        const existingPaths = new Set(state.selectedFiles.map(f => f.path));
        const added = [];
        for (const f of newFiles) {
            if (!existingPaths.has(f.path)) {
                state.selectedFiles.push(f);
                added.push(f);
            }
        }
        renderFileList();
        showToast(`找到 ${result.files.length} 个视频文件，新增 ${added.length} 个`, 'success');
    } catch (e) { showToast(`扫描失败: ${e.message}`, 'error'); }
    finally { setButtonLoading(el.scanDirBtn, false); }
}

function toggleSelectAllFiles() {
    const checked = el.selectAllFiles.checked;
    state.selectedFiles.forEach(f => f.checked = checked);
    document.querySelectorAll('#selectedFilesList .file-checkbox').forEach(cb => cb.checked = checked);
}

async function processSelectedFiles() {
    const checked = state.selectedFiles.filter(f => f.checked);
    let files = checked.map(f => f.path);
    if (files.length === 0) {
        const singlePath = el.manualFilePath.value.trim();
        if (singlePath) {
            files = [singlePath];
        } else {
            showToast('请选择要处理的文件，或输入文件路径', 'warning');
            return;
        }
    }
    setButtonLoading(el.processSelectedBtn, true);
    try {
        const result = await processBatchViaApi(files);
        showToast(result.message, 'success');
        if (result.success) {
            state.selectedFiles = [];
            renderFileList();
            loadTasks();
        }
    } catch (e) { showToast(`批量处理失败: ${e.message}`, 'error'); }
    finally { setButtonLoading(el.processSelectedBtn, false); }
}

// ===== 下载器 =====

async function loadDownloaders() {
    try {
        const result = await loadDownloadersFromApi();
        if (!result.downloaders || result.downloaders.length === 0) {
            el.downloaderList.innerHTML = `<tr><td colspan="3"><div class="empty-state"><p>暂无下载器</p></div></td></tr>`;
            return;
        }
        el.downloaderList.innerHTML = result.downloaders.map(d => `<tr>
            <td>${escapeHtml(d.name || d.id || d.type)}</td>
            <td>${escapeHtml(d.type)}</td>
            <td><span class="badge ${d.connected ? 'badge-success' : 'badge-danger'}">${d.connected ? '已连接' : '未连接'}</span></td>
        </tr>`).join('');
    } catch (e) { if (!e.message.includes('登录已过期')) console.error('加载下载器失败:', e); }
    try {
        await loadDownloaderConfigs();
    } catch (e) { /* ignore */ }
}

// ===== 下载器配置管理（INI 中的 downloader.* 节）=====

const KNOWN_DOWNLOADER_TYPES = ['aria2', 'qbittorrent'];

// 从 downloader.<id> 节名推导下载器类型，支持同一类型多个实例（如 aria2_2 -> aria2）
function deriveDownloaderType(id) {
    const text = String(id || '').trim().toLowerCase();
    if (!text) return '';
    if (KNOWN_DOWNLOADER_TYPES.includes(text)) return text;
    const base = text.replace(/[\-_.]?\d+$/, '');
    return base || text;
}

// 为新增下载器生成唯一标识（aria2、aria2_2、aria2_3 ...）
function nextDownloaderId(baseType, existingIds) {
    const ids = new Set((existingIds || []).map(x => String(x).toLowerCase()));
    if (!ids.has(baseType)) return baseType;
    let n = 2;
    while (ids.has(`${baseType}_${n}`)) n++;
    return `${baseType}_${n}`;
}

async function loadDownloaderConfigs() {
    try {
        const config = await loadConfigFromApi();
        const downloaders = Object.keys(config || {})
            .filter(k => k.startsWith('downloader.'))
            .map(k => {
                const id = k.slice('downloader.'.length);
                const section = config[k] || {};
                return { section: k, id, type: section.type || deriveDownloaderType(id), ...section };
            });
        renderDownloaderConfigs(downloaders);
        if (_configCurrentSection === '__downloaders') renderDownloaderManager();
    } catch (e) {
        if (!e.message.includes('登录已过期')) console.error('加载下载器配置失败:', e);
    }
}

const RPC_PATHS = { aria2:'/jsonrpc', qbittorrent:'/api/v2', transmission:'/transmission/rpc', rtorrent:'/RPC2', deluge:'/json' };

let _downloaderConfigs = [];

const DOWNLOADER_TABLE_HEAD = '<thead><tr><th>名称</th><th>类型</th><th>主机</th><th>端口</th><th>用户名</th><th>RPC 地址</th><th style="width:150px">操作</th></tr></thead>';

function downloaderRowsHtml(downloaders) {
    if (!downloaders || !downloaders.length) {
        return '<tr><td colspan="7"><div class="empty-state"><p>暂无配置，点击「添加下载器」新增</p></div></td></tr>';
    }
    return downloaders.map(d => {
        const name = d.name || d.id || d.section.replace('downloader.', '');
        const type = d.type || deriveDownloaderType(d.id);
        const host = d.host || '-';
        const port = d.port || '-';
        const user = d.username || '-';
        const rpc = d.rpc_url || (host !== '-' && port !== '-' ? `http://${host}:${port}${RPC_PATHS[type] || ''}` : '-');
        return `<tr>
            <td><strong>${escapeHtml(name)}</strong><br><code style="font-size:0.7rem;color:var(--text-muted)">downloader.${escapeHtml(d.id || '')}</code></td>
            <td>${escapeHtml(type)}</td>
            <td>${escapeHtml(host)}</td>
            <td>${escapeHtml(port)}</td>
            <td>${escapeHtml(user)}</td>
            <td style="font-size:0.75rem;font-family:monospace;max-width:220px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(rpc)}</td>
            <td><div class="btn-cell">
                <button class="save-btn" onclick='editDownloaderConfig(${JSON.stringify(d).replace(/'/g,"&#39;")})'>编辑</button>
                <button class="del-btn" onclick="deleteDownloaderConfig('${escapeHtml(d.section)}')">删除</button>
            </div></td>
        </tr>`;
    }).join('');
}

function renderDownloaderConfigs(downloaders) {
    _downloaderConfigs = downloaders || [];
    const rows = downloaderRowsHtml(_downloaderConfigs);
    if (el.downloaderConfigList) el.downloaderConfigList.innerHTML = rows;
    const cfgList = document.getElementById('cfg-downloader-list');
    if (cfgList) cfgList.innerHTML = rows;
    const badge = document.getElementById('dlCount');
    if (badge) badge.textContent = String(_downloaderConfigs.length);
}

// 在「配置管理 → 下载器配置」中在线增删改下载器
function renderDownloaderManager() {
    el.configEditor.innerHTML = `<div class="config-section"><div class="config-section-title">下载器配置</div>
        <div style="margin-bottom:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <button class="add-btn" onclick="showAddDownloaderModal()">+ 添加下载器</button>
            <span style="font-size:0.8125rem;color:var(--text-muted)">支持多个 aria2 实例（标识如 aria2、aria2_2），保存后立即生效，无需重启容器。</span>
        </div>
        <div class="config-table-wrap"><table class="config-table">${DOWNLOADER_TABLE_HEAD}<tbody id="cfg-downloader-list">${downloaderRowsHtml(_downloaderConfigs)}</tbody></table></div>
    </div>`;
}

function editDownloaderConfig(data) {
    const section = data.section || '';
    const id = data.id || (section ? section.replace('downloader.', '') : '');
    const type = data.type || deriveDownloaderType(id) || 'aria2';
    el.modalTitle.textContent = (section ? '编辑下载器 — ' : '添加下载器 — ') + type;
    el.modalBody.innerHTML = `
        <form id="downloaderForm" onsubmit="return false">
            <div class="form-group">
                <label class="form-label">类型</label>
                <select id="dlType" class="form-input">
                    <option value="aria2" ${type==='aria2'?'selected':''}>Aria2</option>
                    <option value="qbittorrent" ${type==='qbittorrent'?'selected':''}>qBittorrent</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">标识（配置节名，同一类型多个实例必须唯一）</label>
                <input type="text" id="dlId" class="form-input" value="${escapeHtml(id)}" placeholder="aria2_2">
            </div>
            <div class="form-group">
                <label class="form-label">名称（显示用，可留空）</label>
                <input type="text" id="dlName" class="form-input" value="${escapeHtml(data.name||'')}" placeholder="我的 Aria2">
            </div>
            <div class="form-group">
                <label class="form-label">主机地址</label>
                <input type="text" id="dlHost" class="form-input" value="${escapeHtml(data.host||'')}" placeholder="localhost">
            </div>
            <div class="form-group">
                <label class="form-label">端口</label>
                <input type="text" id="dlPort" class="form-input" value="${escapeHtml(data.port||'')}" placeholder="6800">
            </div>
            <div class="form-group">
                <label class="form-label">RPC 密钥 / 密码</label>
                <input type="password" id="dlSecret" class="form-input" value="${escapeHtml(data.secret||data.password||'')}" placeholder="">
            </div>
            <div class="form-group">
                <label class="form-label">用户名（qBittorrent）</label>
                <input type="text" id="dlUser" class="form-input" value="${escapeHtml(data.username||'')}" placeholder="">
            </div>
            <div class="form-group">
                <label class="form-label">RPC URL（可选，留空自动拼接）</label>
                <input type="text" id="dlRpcUrl" class="form-input" value="${escapeHtml(data.rpc_url||'')}" placeholder="">
            </div>
        </form>
    `;
    const typeSel = document.getElementById('dlType');
    const idInput = document.getElementById('dlId');
    typeSel.addEventListener('change', () => {
        const cur = idInput.value.trim();
        if (!cur || cur === type) idInput.value = typeSel.value;
    });
    el.modalConfirmBtn.textContent = '保存';
    el.modalConfirmBtn.onclick = async () => {
        const newType = typeSel.value;
        let newId = idInput.value.trim().replace(/[^A-Za-z0-9_\-]/g, '');
        if (!newId) newId = newType;
        const values = { type: newType };
        const name = document.getElementById('dlName').value.trim();
        const host = document.getElementById('dlHost').value.trim();
        const port = document.getElementById('dlPort').value.trim();
        const user = document.getElementById('dlUser').value.trim();
        const secret = document.getElementById('dlSecret').value.trim();
        const rpc = document.getElementById('dlRpcUrl').value.trim();
        if (name) values.name = name;
        if (host) values.host = host;
        if (port) values.port = port;
        if (user) values.username = user;
        if (secret) {
            values.secret = secret;
            values.password = secret;
        }
        if (rpc) { values.rpc_url = rpc; }
        else if (host && port) { values.rpc_url = `http://${host}:${port}${RPC_PATHS[newType] || ''}`; }
        const newSection = 'downloader.' + newId;
        try {
            await saveConfigToApi(newSection, values);
            if (section && section !== newSection) await deleteConfigSectionApi(section);
            showToast('下载器配置已保存', 'success');
            hideModal();
            await loadDownloaderConfigs();
            await loadDownloaders();
        } catch (e) { showToast(`保存失败: ${e.message}`, 'error'); }
    };
    showModal();
}
window.editDownloaderConfig = editDownloaderConfig;

async function showAddDownloaderModal() {
    let existingIds = [];
    try {
        const config = await loadConfigFromApi();
        existingIds = Object.keys(config || {})
            .filter(k => k.startsWith('downloader.'))
            .map(k => k.slice('downloader.'.length));
    } catch (e) { /* ignore */ }
    const id = nextDownloaderId('aria2', existingIds);
    editDownloaderConfig({ section: '', id, type: 'aria2' });
    el.modalTitle.textContent = '添加下载器';
    el.modalConfirmBtn.textContent = '添加';
}
window.showAddDownloaderModal = showAddDownloaderModal;

async function deleteDownloaderConfig(section) {
    if (!confirm(`确定删除下载器配置「${section}」？`)) return;
    try {
        await deleteConfigSectionApi(section);
        showToast('下载器配置已删除', 'success');
        await loadDownloaderConfigs();
        await loadDownloaders();
    } catch (e) { showToast(`删除失败: ${e.message}`, 'error'); }
}
window.deleteDownloaderConfig = deleteDownloaderConfig;

// ===== 仪表盘 WebSocket（带指数退避重连）=====

const dashboardState = { ws: null, reconnectTimer: null, reconnectAttempt: 0, isConnected: false };

function connectDashboardWebSocket() {
    if (dashboardState.ws) return;
    try {
        dashboardState.ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/tasks/ws/dashboard`);
        dashboardState.ws.onopen = () => {
            const token = getAuthToken();
            if (token) dashboardState.ws.send(JSON.stringify({ type: 'auth', token }));
            dashboardState.isConnected = true;
            dashboardState.reconnectAttempt = 0;
        };
        dashboardState.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'snapshot' || data.type === 'update') {
                    handleDashboardUpdate(data);
                }
            } catch (e) { console.error('[Dashboard] 解析消息失败:', e); }
        };
        dashboardState.ws.onclose = () => {
            dashboardState.isConnected = false;
            dashboardState.ws = null;
            dashboardScheduleReconnect();
        };
        dashboardState.ws.onerror = () => {};
    } catch (e) { console.error('[Dashboard] 连接失败:', e); dashboardScheduleReconnect(); }
}

function dashboardScheduleReconnect() {
    if (dashboardState.reconnectTimer) return;
    const delay = Math.min(1000 * Math.pow(2, dashboardState.reconnectAttempt), 30000);
    dashboardState.reconnectAttempt++;
    dashboardState.reconnectTimer = setTimeout(() => {
        dashboardState.reconnectTimer = null;
        connectDashboardWebSocket();
    }, delay);
}

function disconnectDashboardWebSocket() {
    if (dashboardState.ws) { dashboardState.ws.close(); dashboardState.ws = null; }
    if (dashboardState.reconnectTimer) { clearTimeout(dashboardState.reconnectTimer); dashboardState.reconnectTimer = null; }
}

function handleDashboardUpdate(data) {
    // 更新状态卡片
    if (data.stats) {
        const s = data.stats;
        if (el.statusDot) {
            el.statusDot.className = `status-dot ${state.status && state.status.is_running ? 'running' : 'stopped'}`;
        }
        if (el.statQueue) el.statQueue.textContent = s.queue || 0;
        if (el.statProcessing) el.statProcessing.textContent = s.processing || 0;
        if (el.statCompleted) el.statCompleted.textContent = s.completed || 0;
        if (el.statFailed) el.statFailed.textContent = s.failed || 0;
        if (el.queueCount) el.queueCount.textContent = s.queue || 0;
        if (state.status) {
            state.status.queue_size = s.queue || 0;
            state.status.processing_count = s.processing || 0;
            state.status.completed_count = s.completed || 0;
            state.status.failed_count = s.failed || 0;
        }
    }
    // 更新最近活动
    if (data.recent) {
        const items = data.recent.items || [];
        state.recentTotal = data.recent.total || 0;
        if (items.length === 0) {
            el.recentActivity.innerHTML = `<div class="empty-state"><p>暂无数据</p></div>`;
        } else {
            el.recentActivity.innerHTML = items.map((t, i) => `<div class="activity-item" style="animation-delay:${i * 30}ms">
                <div class="activity-dot ${t.status === 'completed' ? 'activity-dot-success' : 'activity-dot-danger'}"></div>
                <div class="activity-path" title="${escapeHtml(t.path)}">${escapeHtml(t.path)}</div>
                <div class="activity-status ${t.status === 'completed' ? 'activity-status-success' : 'activity-status-danger'}">${t.status === 'completed' ? '已完成' : '失败'}</div>
                <div class="activity-time">${formatRelativeTime(t.time)}</div>
            </div>`).join('');
        }
        // 重置分页到第一页
        state.recentPage = 1;
        if (el.recentTotal) el.recentTotal.textContent = data.recent.total || 0;
        if (el.recentPage) el.recentPage.textContent = 1;
        const recentPs = state.recentPageSize || 20;
        if (el.recentTotalPages) {
            const totalPages = Math.ceil((data.recent.total || 0) / recentPs) || 1;
            el.recentTotalPages.textContent = totalPages;
        }
        if (el.recentPrevBtn) el.recentPrevBtn.disabled = true;
        if (el.recentNextBtn) {
            const totalPages = Math.ceil((data.recent.total || 0) / recentPs) || 1;
            el.recentNextBtn.disabled = 1 >= totalPages;
        }
        if (el.recentPagination) el.recentPagination.style.display = (data.recent.total || 0) > 0 ? 'flex' : 'none';
    }
}

// ===== 上传进度 WebSocket（带指数退避重连）=====

const uploadState = { progresses: {}, ws: null, reconnectTimer: null, reconnectAttempt: 0, isConnected: false };
const WS_BASE_DELAY = 1000;
const WS_MAX_DELAY = 30000;

function connectUploadProgressWebSocket() {
    if (uploadState.ws) return;
    try {
        uploadState.ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/tasks/ws/progress`);
        uploadState.ws.onopen = () => {
            const token = getAuthToken();
            if (token) uploadState.ws.send(JSON.stringify({ type: 'auth', token }));
            uploadState.isConnected = true;
            uploadState.reconnectAttempt = 0;
            updateUploadStatusDot(true);
            loadUploadProgress();
        };
        uploadState.ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                if (data.type === 'progress') handleUploadProgressUpdate(data);
            } catch (e) { console.error('[Upload] 解析消息失败:', e); }
        };
        uploadState.ws.onclose = () => {
            uploadState.isConnected = false;
            updateUploadStatusDot(false);
            uploadState.ws = null;
            scheduleReconnect();
        };
        uploadState.ws.onerror = () => {};
    } catch (e) { console.error('[Upload] 连接失败:', e); scheduleReconnect(); }
}

function scheduleReconnect() {
    if (uploadState.reconnectTimer) return;
    const delay = Math.min(WS_BASE_DELAY * Math.pow(2, uploadState.reconnectAttempt), WS_MAX_DELAY);
    uploadState.reconnectAttempt++;
    uploadState.reconnectTimer = setTimeout(() => {
        uploadState.reconnectTimer = null;
        connectUploadProgressWebSocket();
    }, delay);
}

function disconnectUploadProgressWebSocket() {
    if (uploadState.ws) { uploadState.ws.close(); uploadState.ws = null; }
    if (uploadState.reconnectTimer) { clearTimeout(uploadState.reconnectTimer); uploadState.reconnectTimer = null; }
}

async function loadUploadProgress() {
    try {
        const result = await loadUploadProgressFromApi();
        if (result && result.progresses) {
            uploadState.progresses = result.progresses;
            updateUploadProgressList();
        }
    } catch (e) { console.error('[Upload] 加载进度失败:', e); }
}

function handleUploadProgressUpdate(data) {
    const { file_path, filename, uploader, progress, uploaded_bytes, total_bytes, speed, status, error } = data;
    uploadState.progresses[file_path] = { filename, uploader, progress, uploaded_bytes, total_bytes, speed, status, error, timestamp: Date.now() };
    if (status === 'completed' || status === 'failed') {
        setTimeout(() => { delete uploadState.progresses[file_path]; updateUploadProgressList(); }, 5000);
    }
    updateUploadProgressList();
}

function updateUploadStatusDot(isActive) {
    if (el.uploadStatusDot) {
        el.uploadStatusDot.className = isActive ? 'upload-status-dot active' : 'upload-status-dot';
    }
}

function updateUploadProgressList() {
    if (!el.uploadProgressList) return;
    const progresses = Object.values(uploadState.progresses);
    const activeUploads = progresses.filter(p => p.status === 'uploading');
    updateUploadStatusDot(activeUploads.length > 0);

    if (progresses.length === 0) {
        el.uploadProgressList.innerHTML = `<tr><td colspan="5"><div class="empty-state"><p>暂无上传任务</p></div></td></tr>`;
        return;
    }

    el.uploadProgressList.innerHTML = progresses.map(p => {
        const statusBadge = p.status === 'uploading' ? '<span class="badge badge-warning">上传中</span>'
            : p.status === 'completed' ? '<span class="badge badge-success">已完成</span>'
            : p.status === 'failed' ? '<span class="badge badge-danger">失败</span>' : '';
        const uploaderNames = { 'emos': 'Emos' };
        const pct = Math.min(100, Math.max(0, p.progress || 0));
        return `<tr>
            <td style="font-family:monospace;font-size:0.8125rem">${escapeHtml(p.filename || '未知文件')}</td>
            <td><span class="uploader-badge uploader-${p.uploader}">${escapeHtml(uploaderNames[p.uploader] || p.uploader)}</span></td>
            <td style="min-width:150px">
                <div class="progress-bar-container"><div class="progress-bar-fill" style="width:${pct}%"></div></div>
                <div class="progress-text">${formatSize(p.uploaded_bytes || 0)} / ${formatSize(p.total_bytes || 0)}</div>
                <small style="color:var(--text-muted)">${pct.toFixed(1)}%</small>
            </td>
            <td>${escapeHtml(p.speed || '-')}</td>
            <td>${statusBadge}${p.error ? `<br><small style="color:var(--accent-danger)">${escapeHtml(p.error)}</small>` : ''}</td>
        </tr>`;
    }).join('');
}

// ===== 用户管理 =====

let _users = [];

async function loadUsers() {
    try {
        _users = await loadUsersFromApi();
        renderUsers();
    } catch (e) { if (!e.message.includes('登录已过期')) console.error('加载用户失败:', e); }
}

function renderUsers() {
    if (!_users.length) {
        el.userList.innerHTML = `<tr><td colspan="6"><div class="empty-state"><p>暂无用户</p></div></td></tr>`;
        return;
    }
    el.userList.innerHTML = _users.map(u => `<tr>
        <td>${u.id}</td>
        <td><strong>${escapeHtml(u.username)}</strong></td>
        <td><span class="badge ${u.role === 'admin' ? 'badge-success' : 'badge-info'}">${escapeHtml(u.role)}</span></td>
        <td><span class="badge ${u.enabled ? 'badge-success' : 'badge-danger'}">${u.enabled ? '启用' : '禁用'}</span></td>
        <td style="font-size:0.8125rem;color:var(--text-muted)">${u.created_at ? new Date(u.created_at).toLocaleString() : '-'}</td>
        <td><div class="btn-cell">
            <button class="save-btn" onclick="editUser('${u.id}')">编辑</button>
            <button class="del-btn" onclick="deleteUser('${u.id}')">删除</button>
        </div></td>
    </tr>`).join('');
}

function showAddUserModal() {
    el.modalTitle.textContent = '添加用户';
    el.modalBody.innerHTML = `
        <form id="userForm" onsubmit="return false">
            <div class="form-group">
                <label class="form-label">用户名</label>
                <input type="text" id="ufUsername" class="form-input" placeholder="请输入用户名" autocomplete="off">
            </div>
            <div class="form-group">
                <label class="form-label">密码</label>
                <input type="password" id="ufPassword" class="form-input" placeholder="请输入密码" autocomplete="new-password">
            </div>
            <div class="form-group">
                <label class="form-label">角色</label>
                <select id="ufRole" class="form-input">
                    <option value="user">普通用户</option>
                    <option value="admin">管理员</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label"><input type="checkbox" id="ufEnabled" checked> 启用</label>
            </div>
        </form>
    `;
    el.modalConfirmBtn.textContent = '添加';
    el.modalConfirmBtn.onclick = async () => {
        const username = document.getElementById('ufUsername').value.trim();
        const password = document.getElementById('ufPassword').value.trim();
        const role = document.getElementById('ufRole').value;
        const enabled = document.getElementById('ufEnabled').checked;
        if (!username) { showToast('请输入用户名', 'error'); return; }
        if (!password) { showToast('请输入密码', 'error'); return; }
        try {
            await createUserViaApi({ username, password, role, enabled });
            showToast('用户已创建', 'success');
            hideModal();
            await loadUsers();
        } catch (e) { showToast('创建失败: ' + e.message, 'error'); }
    };
    showModal();
}
window.showAddUserModal = showAddUserModal;

async function editUser(userId) {
    const u = _users.find(x => x.id == userId);
    if (!u) return;
    el.modalTitle.textContent = '编辑用户 — ' + u.username;
    el.modalBody.innerHTML = `
        <form id="userForm" onsubmit="return false">
            <div class="form-group">
                <label class="form-label">用户名</label>
                <input type="text" id="ufUsername" class="form-input" value="${escapeHtml(u.username)}" autocomplete="off">
            </div>
            <div class="form-group">
                <label class="form-label">密码（留空不修改）</label>
                <input type="password" id="ufPassword" class="form-input" placeholder="留空则保持原密码" autocomplete="new-password">
            </div>
            <div class="form-group">
                <label class="form-label">角色</label>
                <select id="ufRole" class="form-input">
                    <option value="user" ${u.role === 'user' ? 'selected' : ''}>普通用户</option>
                    <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>管理员</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label"><input type="checkbox" id="ufEnabled" ${u.enabled ? 'checked' : ''}> 启用</label>
            </div>
        </form>
    `;
    el.modalConfirmBtn.textContent = '保存';
    el.modalConfirmBtn.onclick = async () => {
        const data = {};
        const username = document.getElementById('ufUsername').value.trim();
        const password = document.getElementById('ufPassword').value.trim();
        const role = document.getElementById('ufRole').value;
        const enabled = document.getElementById('ufEnabled').checked;
        if (!username) { showToast('请输入用户名', 'error'); return; }
        data.username = username;
        if (password) data.password = password;
        data.role = role;
        data.enabled = enabled;
        try {
            await updateUserViaApi(userId, data);
            showToast('用户已更新', 'success');
            hideModal();
            await loadUsers();
        } catch (e) { showToast('更新失败: ' + e.message, 'error'); }
    };
    showModal();
}
window.editUser = editUser;

async function deleteUser(userId) {
    const u = _users.find(x => x.id == userId);
    if (!confirm('确定删除用户「' + (u ? u.username : userId) + '」？')) return;
    try {
        await deleteUserViaApi(userId);
        showToast('用户已删除', 'success');
        await loadUsers();
    } catch (e) { showToast('删除失败: ' + e.message, 'error'); }
}
window.deleteUser = deleteUser;
// ===== 在线识别上传 =====

const onlineState = {
    initialized: false,
    config: null,
    path: '',
    videos: [],
    selected: new Set(),
    recognized: [],
    tasks: [],
    timer: null,
};

function escapeOnline(value) {
    return escapeHtml(String(value == null ? '' : value));
}

async function initOnlinePage() {
    if (!onlineState.initialized) {
        onlineState.initialized = true;
        bindOnlineEvents();
    }
    startOnlinePolling();
    await loadOnlineConfig();
    await loadOnlineTasks();
    await loadTelegramStatus();
}

function bindOnlineEvents() {
    const bind = (id, handler) => {
        const node = document.getElementById(id);
        if (node) node.addEventListener('click', handler);
    };
    bind('onlineRefreshBtn', async () => { await loadOnlineConfig(); await loadOnlineTasks(); await loadTelegramStatus(); });
    bind('onlineTgRefreshBtn', () => loadTelegramStatus());
    bind('onlineTgTestBtn', () => sendTelegramTest());
    bind('onlineBrowseBtn', () => browseOnline(onlineState.path));
    bind('onlineScanBtn', () => scanOnline());
    bind('onlineRecognizeBtn', () => recognizeSelected());
    bind('onlineRefreshTasksBtn', () => loadOnlineTasks());
    bind('onlineClearTasksBtn', async () => {
        try { await clearOnlineTasksApi(); await loadOnlineTasks(); }
        catch (e) { alert('清空失败: ' + e.message); }
    });

    const selectAll = document.getElementById('onlineSelectAll');
    if (selectAll) selectAll.addEventListener('change', () => {
        onlineState.selected = new Set(selectAll.checked ? onlineState.videos.map(v => v.path) : []);
        renderOnlineVideos();
    });

    const pathInput = document.getElementById('onlinePathInput');
    if (pathInput) pathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            onlineState.path = pathInput.value.trim();
            browseOnline(onlineState.path);
        }
    });

    bind('onlineSearchBtn', () => searchOnlineTargets());
    const searchInput = document.getElementById('onlineSearchInput');
    if (searchInput) searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') searchOnlineTargets();
    });
}

function stopOnlinePolling() {
    if (onlineState.timer) { clearInterval(onlineState.timer); onlineState.timer = null; }
}

function startOnlinePolling() {
    stopOnlinePolling();
    onlineState.timer = setInterval(() => {
        if (document.querySelector('#page-online.active')) loadOnlineTasks();
    }, 3000);
}

async function loadOnlineConfig() {
    try {
        const data = await loadOnlineConfigApi();
        onlineState.config = data;
        const hint = document.getElementById('onlineRootHint');
        if (hint) {
            const roots = (data.roots || []).join(' 、 ') || '未配置视频根目录';
            const tokenText = data.token_configured
                ? '已配置 Emos Token'
                : '<span style="color:#e5534b">未配置 Emos auth_token，无法识别/上传</span>';
            hint.innerHTML = 'Emos: <code>' + escapeOnline(data.base_url || '') + '</code> · ' + tokenText +
                ' · 可浏览根目录: ' + escapeOnline(roots);
        }
        const input = document.getElementById('onlinePathInput');
        if (input && !input.value) {
            input.value = data.video_root || (data.roots || [])[0] || '';
        }
        onlineState.path = input ? input.value.trim() : '';
    } catch (e) {
        console.error('加载在线上传配置失败:', e);
    }
}

async function browseOnline(path) {
    try {
        const data = await browseOnlineApi(path || '');
        const panel = document.getElementById('onlineDirPanel');
        const list = document.getElementById('onlineDirList');
        if (!panel || !list) return;
        const entries = data.entries || [];
        let html = '';
        if (data.parent) {
            html += '<tr><td colspan="4"><a href="#" data-online-dir="' + escapeOnline(data.parent) + '">⬆ 上级目录</a></td></tr>';
        }
        for (const entry of entries) {
            const isDir = entry.kind === 'directory' || entry.kind === 'root';
            const action = isDir
                ? '<a href="#" data-online-dir="' + escapeOnline(entry.path) + '">打开</a>'
                : '<a href="#" data-online-probe="' + escapeOnline(entry.path) + '">探测</a>';
            html += '<tr><td>' + (isDir ? '📁 ' : '🎬 ') + escapeOnline(entry.name) + '</td>' +
                '<td>' + (isDir ? '目录' : '视频') + '</td>' +
                '<td>' + escapeOnline(entry.size_text || '') + '</td>' +
                '<td>' + action + '</td></tr>';
        }
        if (!html) html = '<tr><td colspan="4"><div class="empty-state"><p>空目录</p></div></td></tr>';
        list.innerHTML = html;
        panel.style.display = '';
        list.querySelectorAll('[data-online-dir]').forEach(node => node.addEventListener('click', (e) => {
            e.preventDefault();
            const target = node.getAttribute('data-online-dir');
            const input = document.getElementById('onlinePathInput');
            if (input) input.value = target;
            onlineState.path = target;
            browseOnline(target);
        }));
        list.querySelectorAll('[data-online-probe]').forEach(node => node.addEventListener('click', (e) => {
            e.preventDefault();
            probeOnline(node.getAttribute('data-online-probe'));
        }));
    } catch (e) {
        alert('浏览失败: ' + e.message);
    }
}

async function scanOnline() {
    const input = document.getElementById('onlinePathInput');
    const recursive = document.getElementById('onlineRecursiveCheck');
    const path = input ? input.value.trim() : '';
    try {
        const data = await scanOnlineApi(path, recursive ? recursive.checked : true);
        onlineState.path = data.path || path;
        onlineState.videos = data.files || [];
        onlineState.selected = new Set();
        renderOnlineVideos();
        const panel = document.getElementById('onlineVideoPanel');
        if (panel) panel.style.display = '';
        if (!onlineState.videos.length) alert('该目录下没有扫描到视频文件');
    } catch (e) {
        alert('扫描失败: ' + e.message);
    }
}

function renderOnlineVideos() {
    const list = document.getElementById('onlineVideoList');
    const count = document.getElementById('onlineVideoCount');
    if (!list) return;
    const videos = onlineState.videos;
    if (!videos.length) {
        list.innerHTML = '<tr><td colspan="4"><div class="empty-state"><p>暂无视频</p></div></td></tr>';
    } else {
        list.innerHTML = videos.map(v =>
            '<tr>' +
                '<td><input type="checkbox" data-online-video="' + escapeOnline(v.path) + '"' +
                    (onlineState.selected.has(v.path) ? ' checked' : '') + '></td>' +
                '<td>' + escapeOnline(v.name) +
                    '<div style="font-size:12px;color:var(--text-muted)">' + escapeOnline(v.path) + '</div></td>' +
                '<td>' + escapeOnline(v.size_text || '') + '</td>' +
                '<td><a href="#" data-online-probe="' + escapeOnline(v.path) + '">探测</a></td>' +
            '</tr>').join('');
        list.querySelectorAll('[data-online-video]').forEach(node => node.addEventListener('change', () => {
            const p = node.getAttribute('data-online-video');
            if (node.checked) onlineState.selected.add(p); else onlineState.selected.delete(p);
            const counter = document.getElementById('onlineVideoCount');
            if (counter) counter.textContent = '已选择 ' + onlineState.selected.size + ' / ' + onlineState.videos.length;
        }));
        list.querySelectorAll('[data-online-probe]').forEach(node => node.addEventListener('click', (e) => {
            e.preventDefault();
            probeOnline(node.getAttribute('data-online-probe'));
        }));
    }
    if (count) count.textContent = '已选择 ' + onlineState.selected.size + ' / ' + videos.length;
    const selectAll = document.getElementById('onlineSelectAll');
    if (selectAll) selectAll.checked = videos.length > 0 && onlineState.selected.size === videos.length;
}

async function probeOnline(path) {
    const card = document.getElementById('onlineProbeCard');
    const box = document.getElementById('onlineProbeContent');
    if (!card || !box) return;
    try {
        const data = await probeOnlineApi(path);
        const s = data.summary || {};
        const skipped = data.skipped ? '（已按配置跳过 ffprobe 校验）' : '';
        const available = data.available === false && !data.skipped ? '（未安装 ffprobe，可通过配置 ffprobe_path 指定路径）' : '';
        box.innerHTML = '<div class="config-section">' +
            '<div style="font-size:13px">文件: <code>' + escapeOnline(path) + '</code></div>' +
            '<div style="font-size:13px;margin-top:6px">状态: ' +
                (data.valid ? '✅ 有效视频' : '⚠️ ' + escapeOnline(data.error || '无法解析')) +
                escapeOnline(skipped) + escapeOnline(available) + '</div>' +
            '<div style="font-size:13px;margin-top:6px">分辨率: ' +
                escapeOnline((s.width || 0) + 'x' + (s.height || 0)) +
                ' · 编码: ' + escapeOnline((s.video_codec || '-') + '/' + (s.audio_codec || '-')) +
                ' · 时长: ' + escapeOnline(Math.round(s.duration || 0)) + 's' +
                ' · 帧率: ' + escapeOnline(s.frame_rate || '-') +
                ' · 动态范围: ' + escapeOnline(s.dynamic_range || '-') + '</div>' +
            '</div>';
        card.style.display = '';
    } catch (e) {
        alert('探测失败: ' + e.message);
    }
}

async function recognizeSelected() {
    const paths = Array.from(onlineState.selected);
    if (!paths.length) { alert('请先勾选要识别的视频'); return; }
    const btn = document.getElementById('onlineRecognizeBtn');
    if (btn) { btn.disabled = true; btn.textContent = '识别中...'; }
    const results = [];
    for (const p of paths) {
        try {
            results.push(await recognizeOnlineApi(p));
        } catch (e) {
            results.push({
                file_path: p,
                file_name: p.replace(/^.*[\\/]/, ''),
                metadata: {}, candidates: [], match: null, error: e.message,
            });
        }
    }
    onlineState.recognized = results.map(buildRecognizedItem);
    renderOnlineRecognize();
    if (btn) { btn.disabled = false; btn.textContent = '识别选中文件'; }
}

function buildRecognizedItem(data) {
    const target = data.match ? Object.assign({}, data.match) : null;
    const options = [];
    const seen = new Set();
    const push = (t) => {
        if (!t || !t.item_id) return;
        const key = t.item_type + ':' + t.item_id;
        if (seen.has(key)) return;
        seen.add(key);
        options.push({
            item_type: t.item_type,
            item_id: String(t.item_id),
            label: t.label || key,
            kind: t.kind || '',
        });
    };
    push(target);
    for (const video of (data.candidates || [])) {
        push({ item_type: 'vl', item_id: video.item_id, label: '[作品] ' + (video.title || ''), kind: 'video' });
        for (const season of (video.seasons || [])) {
            push({ item_type: 'vs', item_id: season.item_id, label: '　└ S' + (season.season_number || '?') + ' ' + (season.season_title || ''), kind: 'season' });
            for (const ep of (season.episodes || [])) {
                push({ item_type: 've', item_id: ep.item_id, label: '　　└ S' + (season.season_number || '?') + 'E' + (ep.episode_number || '?') + ' ' + (ep.episode_title || ''), kind: 'episode' });
            }
        }
    }
    return {
        file_path: data.file_path,
        file_name: data.file_name || String(data.file_path || '').replace(/^.*[\\/]/, ''),
        file_size: data.file_size || 0,
        metadata: data.metadata || {},
        match: data.match || null,
        candidates: data.candidates || [],
        options: options,
        target: target,
        storage: '',
        error: data.error || '',
    };
}

function renderOnlineRecognize() {
    const card = document.getElementById('onlineRecognizeCard');
    const box = document.getElementById('onlineRecognizeResult');
    if (!card || !box) return;
    const items = onlineState.recognized;
    if (!items.length) { card.style.display = 'none'; box.innerHTML = ''; return; }
    card.style.display = '';

    const storages = (onlineState.config && onlineState.config.storages) || ['internal'];
    const defaultStorage = (onlineState.config && onlineState.config.default_storage) || 'internal';

    let html = '<div class="config-section">';
    items.forEach((item, index) => {
        const meta = item.metadata || {};
        const optionHtml = item.options.length
            ? item.options.map((t, i) =>
                '<option value="' + i + '"' +
                (item.target && item.target.item_type === t.item_type && String(item.target.item_id) === String(t.item_id) ? ' selected' : '') +
                '>' + escapeOnline(t.label) + '（' + escapeOnline(t.item_type + '/' + t.item_id) + '）</option>').join('')
            : '<option value="-1">未找到匹配，请使用上方搜索</option>';
        const storageHtml = storages.map(s =>
            '<option value="' + escapeOnline(s) + '"' + (s === (item.storage || defaultStorage) ? ' selected' : '') + '>' + escapeOnline(s) + '</option>').join('');
        const seasonText = (meta.season != null ? ' · S' + meta.season : '') + (meta.episode != null ? 'E' + meta.episode : '');
        html += '<div style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px">' +
            '<div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">' +
                '<div style="min-width:260px">' +
                    '<div style="font-weight:600">' + escapeOnline(item.file_name) + '</div>' +
                    '<div style="font-size:12px;color:var(--text-muted)">' + escapeOnline(item.file_path) + '</div>' +
                    '<div style="font-size:12px;color:var(--text-muted);margin-top:4px">识别: ' +
                        escapeOnline(meta.title || '-') + ' · TMDB: ' + escapeOnline(meta.tmdb_id || '-') +
                        ' · 类型: ' + escapeOnline(meta.media_type || '-') + escapeOnline(seasonText) + '</div>' +
                    (item.error ? '<div style="font-size:12px;color:#e5534b;margin-top:4px">' + escapeOnline(item.error) + '</div>' : '') +
                '</div>' +
                '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' +
                    '<select class="form-input" data-online-target="' + index + '" style="min-width:260px">' + optionHtml + '</select>' +
                    '<select class="form-input" data-online-storage="' + index + '" style="min-width:130px">' + storageHtml + '</select>' +
                    '<button class="btn btn-primary btn-sm" data-online-add="' + index + '">加入上传队列</button>' +
                '</div>' +
            '</div>' +
        '</div>';
    });
    html += '</div>';
    box.innerHTML = html;

    box.querySelectorAll('[data-online-target]').forEach(node => node.addEventListener('change', () => {
        const item = onlineState.recognized[parseInt(node.getAttribute('data-online-target'), 10)];
        if (!item) return;
        const idx = parseInt(node.value, 10);
        item.target = idx >= 0 ? item.options[idx] : null;
    }));
    box.querySelectorAll('[data-online-storage]').forEach(node => node.addEventListener('change', () => {
        const item = onlineState.recognized[parseInt(node.getAttribute('data-online-storage'), 10)];
        if (item) item.storage = node.value;
    }));
    box.querySelectorAll('[data-online-add]').forEach(node => node.addEventListener('click', () => {
        addOnlineTask(parseInt(node.getAttribute('data-online-add'), 10));
    }));
}

async function searchOnlineTargets() {
    const input = document.getElementById('onlineSearchInput');
    const box = document.getElementById('onlineSearchResults');
    const keyword = input ? input.value.trim() : '';
    if (!keyword) { alert('请输入搜索关键词'); return; }
    if (!box) return;
    try {
        const data = await searchOnlineTargetsApi(keyword);
        const results = data.results || [];
        if (!results.length) {
            box.innerHTML = '<div class="empty-state"><p>未搜索到 Emos 条目</p></div>';
            return;
        }
        let html = '<div class="table-container" style="max-height:260px;overflow:auto"><table>' +
            '<thead><tr><th>标题</th><th style="width:100px">类型</th><th style="width:140px">item_id</th><th style="width:120px">操作</th></tr></thead><tbody>';
        results.forEach((item, i) => {
            html += '<tr><td>' + escapeOnline(item.title || '') + '</td>' +
                '<td>' + escapeOnline(item.video_type || '') + '</td>' +
                '<td>' + escapeOnline(item.item_type + '/' + item.item_id) + '</td>' +
                '<td><button class="btn btn-secondary btn-sm" data-online-apply="' + i + '">设为目标</button></td></tr>';
        });
        html += '</tbody></table></div>';
        box.innerHTML = html;
        window._onlineSearchResults = results;
        box.querySelectorAll('[data-online-apply]').forEach(node => node.addEventListener('click', () => {
            applyOnlineSearchResult(parseInt(node.getAttribute('data-online-apply'), 10));
        }));
    } catch (e) {
        alert('搜索失败: ' + e.message);
    }
}

function applyOnlineSearchResult(index) {
    const results = window._onlineSearchResults || [];
    const result = results[index];
    if (!result) return;
    const pending = onlineState.recognized.filter(item => !item.target);
    const targets = pending.length ? pending : onlineState.recognized;
    if (!targets.length) { alert('请先识别视频文件'); return; }
    const applyTo = pending.length ? pending[0] : targets[0];
    const option = {
        item_type: result.item_type || 'vl',
        item_id: String(result.item_id),
        label: result.title || '',
        kind: 'video',
    };
    if (!applyTo.options.some(t => t.item_type === option.item_type && String(t.item_id) === String(option.item_id))) {
        applyTo.options.unshift(option);
    }
    applyTo.target = option;
    renderOnlineRecognize();
}

async function addOnlineTask(index) {
    const item = onlineState.recognized[index];
    if (!item) return;
    if (!item.target) { alert('请先为该文件选择一个上传目标'); return; }
    const meta = item.metadata || {};
    try {
        const result = await createOnlineTasksApi([{
            file_path: item.file_path,
            item_type: item.target.item_type,
            item_id: String(item.target.item_id),
            storage: item.storage || null,
            title: meta.title || '',
            media_type: meta.media_type || '',
            season_number: meta.season == null ? null : meta.season,
            episode_number: meta.episode == null ? null : meta.episode,
        }]);
        if (result.errors && result.errors.length) {
            alert('创建任务失败: ' + result.errors.join('；'));
        }
        await loadOnlineTasks();
    } catch (e) {
        alert('创建任务失败: ' + e.message);
    }
}

async function loadOnlineTasks() {
    try {
        const data = await loadOnlineTasksApi();
        onlineState.tasks = data.tasks || [];
        renderOnlineTasks();
    } catch (e) {
        console.error('加载在线任务失败:', e);
    }
}

async function loadTelegramStatus() {
    const box = document.getElementById('onlineTgStatus');
    if (!box) return;
    try {
        const data = await loadTelegramStatusApi();
        onlineState.telegram = data;
        const parts = [
            data.enabled ? '通知：已启用' : '通知：已禁用',
            data.reply_enabled ? '回复修正：已启用' : '回复修正：已关闭',
            data.token_configured ? 'bot_token：已配置' : 'bot_token：未配置',
            data.bound ? ('已绑定 chat_id：' + data.chat_id) : '未绑定（给机器人发送 /bind）',
            data.running ? '长轮询：运行中' : '长轮询：未运行',
            '待回复报错：' + (data.pending_replies || 0),
        ];
        if (data.last_error) parts.push('最近错误：' + data.last_error);
        box.textContent = parts.join(' · ');
    } catch (e) {
        box.textContent = '获取 Telegram 状态失败: ' + e.message;
    }
}

async function sendTelegramTest() {
    try {
        const data = await sendTelegramTestApi();
        alert(data.success ? '测试消息已发送' : ('发送失败: ' + (data.message || '')));
        await loadTelegramStatus();
    } catch (e) {
        alert('发送失败: ' + e.message);
    }
}

function renderOnlineTasks() {
    const list = document.getElementById('onlineTaskList');
    if (!list) return;
    const tasks = onlineState.tasks;
    if (!tasks.length) {
        list.innerHTML = '<tr><td colspan="6"><div class="empty-state"><p>暂无任务</p></div></td></tr>';
        return;
    }
    const statusMap = { queued: '排队中', uploading: '上传中', completed: '已完成', failed: '失败' };
    list.innerHTML = tasks.map(t => {
        const progress = Math.min(100, Math.max(0, Number(t.progress) || 0));
        const statusText = statusMap[t.status] || t.status;
        const color = t.status === 'completed' ? '#3fb950' : (t.status === 'failed' ? '#e5534b' : 'inherit');
        const actions = (t.status === 'queued' || t.status === 'uploading')
            ? '<span style="font-size:12px;color:var(--text-muted)">进行中…</span>'
            : '<button class="btn btn-secondary btn-sm" data-online-retry="' + escapeOnline(t.id) + '">重试</button> ' +
              '<button class="btn btn-secondary btn-sm" data-online-delete="' + escapeOnline(t.id) + '">删除</button>';
        return '<tr>' +
            '<td>' + escapeOnline(t.file_name) + '<div style="font-size:12px;color:var(--text-muted)">' + escapeOnline(t.file_path) + '</div></td>' +
            '<td>' + escapeOnline(t.item_type + '/' + t.item_id) + '</td>' +
            '<td>' + escapeOnline(t.storage || '') + '</td>' +
            '<td><div class="progress-bar-container"><div class="progress-bar-fill" style="width:' + progress + '%"></div></div>' +
                '<div style="font-size:12px;color:var(--text-muted);margin-top:4px">' + escapeOnline(t.stage || '') + ' ' + progress.toFixed(1) + '%' +
                (t.speed ? ' · ' + escapeOnline(t.speed) : '') + '</div></td>' +
            '<td style="color:' + color + '">' + escapeOnline(statusText) +
                (t.error ? '<div style="font-size:12px">' + escapeOnline(t.error) + '</div>' : '') +
                (t.original_deleted ? '<div style="font-size:12px;color:var(--text-muted)">已删除原文件</div>' : '') + '</td>' +
            '<td>' + actions + '</td>' +
        '</tr>';
    }).join('');

    list.querySelectorAll('[data-online-retry]').forEach(node => node.addEventListener('click', async () => {
        try { await retryOnlineTaskApi(node.getAttribute('data-online-retry')); await loadOnlineTasks(); }
        catch (e) { alert('重试失败: ' + e.message); }
    }));
    list.querySelectorAll('[data-online-delete]').forEach(node => node.addEventListener('click', async () => {
        try { await deleteOnlineTaskApi(node.getAttribute('data-online-delete')); await loadOnlineTasks(); }
        catch (e) { alert('删除失败: ' + e.message); }
    }));
}