console.log("VUZA v5 — English UI, Chinese content language supported");

document.addEventListener('DOMContentLoaded', () => {
    const scrapeBtn = document.getElementById('scrape-btn');
    const queryInput = document.getElementById('query');
    const scriptInput = document.getElementById('script');
    const countInput = document.getElementById('count');
    const statusCard = document.getElementById('status-card');
    const statusMsg = document.getElementById('status-msg');
    const statusPercent = document.getElementById('status-percent');
    const progressFill = document.getElementById('progress-fill');
    const galleryContainer = document.getElementById('gallery-container');
    const clearBtn = document.getElementById('clear-gallery');
    const analyzeBtn = document.getElementById('analyze-btn');
    const generateScriptBtn = document.getElementById('generate-script-btn');
    const regenerateKeywordsBtn = document.getElementById('regenerate-keywords-btn');
    const keywordsInput = document.getElementById('keywords-input');
    const topicInput = document.getElementById('topic-input');
    const analysisPanel = document.getElementById('analysis-panel');
    const aiTitle = document.getElementById('ai-title');
    const aiDesc = document.getElementById('ai-desc');
    const aiHashtags = document.getElementById('ai-hashtags');
    const aiThumbPrompt = document.getElementById('ai-thumb-prompt');

    const tabSingle = document.getElementById('tab-single');
    const tabScript = document.getElementById('tab-script');
    const singleArea = document.getElementById('single-input-area');
    const scriptArea = document.getElementById('script-input-area');
    const scriptsContainer = document.getElementById('scripts-container');
    const addScriptBtn = document.getElementById('add-script-btn');
    const templateSelect = document.getElementById('template-select');
    const scrapeUrlBtn = document.getElementById('scrape-url-btn');
    const urlInput = document.getElementById('url-input');
    const musicSelect = document.getElementById('music-select');
    const llmPreset = document.getElementById('llm-preset');
    const voicePreviewBtn = document.getElementById('voice-preview-btn');

    let currentMode = 'single';
    let statusInterval = null;
    let finalVideoUrl = '';
    let candidateVideos = [];
    let activeTaskId = '';
    let pollConnectionErrorShown = false;
    let allowedMusic = new Set(['none', 'cinematic.mp3']);

    const settingsToggle = document.getElementById('settings-toggle');
    const settingsBody = document.getElementById('settings-body');
    const settingsPanel = document.getElementById('settings-panel');

    if (settingsToggle) {
        settingsToggle.addEventListener('click', () => {
            settingsBody.classList.toggle('hidden');
            settingsPanel.classList.toggle('open');
        });
    }

    let llmPresetsById = {};
    let llmProviders = {};
    let activeProviderId = 'openrouter';

    function currentProviderId() {
        return (llmPreset && llmPreset.value) || 'custom';
    }

    function providerMeta(id) {
        if (!id || id === 'custom') return { id: 'custom', label: 'Custom URL', url: '', models: [] };
        return llmPresetsById[id] || { id, label: id, url: '', models: [] };
    }

    function saveCurrentProvider() {
        const id = activeProviderId || currentProviderId();
        llmProviders[id] = {
            ...(llmProviders[id] || {}),
            key: (document.getElementById('llm-key')?.value || '').trim(),
            model: (document.getElementById('llm-model')?.value || '').trim(),
            url: (document.getElementById('llm-url')?.value || '').trim(),
        };
    }

    function loadProvider(id) {
        activeProviderId = id || 'custom';
        const preset = providerMeta(activeProviderId);
        const saved = llmProviders[activeProviderId] || {};
        const keyLabel = document.getElementById('llm-key-label');
        const modelLabel = document.getElementById('llm-model-label');
        const keyEl = document.getElementById('llm-key');
        const modelEl = document.getElementById('llm-model');
        const urlEl = document.getElementById('llm-url');
        const urlField = document.getElementById('llm-url-field');
        const datalist = document.getElementById('llm-model-presets');
        const statusEl = document.getElementById('llm-test-status');
        if (keyLabel) keyLabel.textContent = `${preset.label} API key`;
        if (modelLabel) modelLabel.textContent = `${preset.label} model`;
        if (keyEl) {
            keyEl.placeholder = providerPlaceholder(activeProviderId);
            keyEl.value = saved.key || '';
        }
        if (modelEl) {
            modelEl.placeholder = preset.model || (preset.models || [])[0] || '';
            modelEl.value = saved.model || preset.model || '';
        }
        if (datalist) {
            datalist.innerHTML = (preset.models || []).map((name) => `<option value="${name}">`).join('');
        }
        if (urlField && urlEl) {
            const isCustom = activeProviderId === 'custom';
            urlField.classList.toggle('hidden', !isCustom);
            urlEl.value = isCustom ? (saved.url || urlEl.value || '') : (preset.url || '');
        }
        if (statusEl) statusEl.textContent = '';
        if (llmPreset) llmPreset.value = activeProviderId === 'custom' ? '' : activeProviderId;
    }

    function loadKeys() {
        const keys = JSON.parse(localStorage.getItem('vuza_api_keys') || '{}');
        llmProviders = { ...(keys.llm_providers || {}) };
        if (keys.llm_preset && llmPreset) llmPreset.value = keys.llm_preset;
        const id = currentProviderId();
        if (keys.llm_key && !(llmProviders[id] && llmProviders[id].key)) {
            llmProviders[id] = {
                ...(llmProviders[id] || {}),
                key: keys.llm_key,
                model: keys.llm_model || '',
                url: keys.llm_url || '',
            };
        }
        if (keys.pexels_key) document.getElementById('pexels-key').value = keys.pexels_key;
        if (keys.pixabay_key) document.getElementById('pixabay-key').value = keys.pixabay_key;
        if (keys.coverr_key) document.getElementById('coverr-key').value = keys.coverr_key;
        if (keys.piapi_key && !keys.piapi_key.startsWith('r8_')) document.getElementById('piapi-key').value = keys.piapi_key;
        if (keys.piapi_model && keys.piapi_model !== 'kling-2.5') document.getElementById('piapi-model').value = keys.piapi_model;
        if (keys.yt_client_id) document.getElementById('yt-client-id').value = keys.yt_client_id;
        if (keys.yt_client_secret) document.getElementById('yt-client-secret').value = keys.yt_client_secret;
        if (keys.eleven_key) document.getElementById('eleven-key').value = keys.eleven_key;
        loadProvider(id);
    }

    function providerPlaceholder(id) {
        if (id === 'ollama') return 'Not required for local Ollama';
        if (id === 'openai') return 'sk-...';
        if (id === 'openrouter') return 'sk-or-v1-...';
        if (id === 'deepseek') return 'sk-...';
        if (id === 'groq') return 'gsk_...';
        return 'API key';
    }

    async function testProviderApi() {
        saveCurrentProvider();
        const id = currentProviderId();
        const preset = providerMeta(id);
        const saved = llmProviders[id] || {};
        const statusEl = document.getElementById('llm-test-status');
        const btn = document.getElementById('test-llm-btn');
        const key = saved.key || '';
        const model = saved.model || preset.model || '';
        const url = id === 'custom' ? (saved.url || '') : (preset.url || '');
        if (statusEl) statusEl.textContent = 'Testing...';
        if (btn) btn.disabled = true;
        try {
            const response = await fetch('/api/llm/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    provider: id === 'custom' ? '' : id,
                    api_key: key,
                    api_url: url,
                    model,
                }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                const message = typeof data.detail === 'string' ? data.detail : 'API test failed';
                if (statusEl) statusEl.textContent = message;
                showToast(message, 'error');
                return;
            }
            persistKeys(getKeys());
            const okMsg = `OK · ${data.model || model}`;
            if (statusEl) statusEl.textContent = okMsg;
            showToast(`${preset.label} ${okMsg}`, 'success');
        } catch (error) {
            const message = 'Could not reach VUZA to test this API';
            if (statusEl) statusEl.textContent = message;
            showToast(message, 'error');
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    async function initLlmProviders() {
        try {
            const response = await fetch('/api/llm/presets');
            const data = await response.json();
            const presets = data.presets || [];
            llmPresetsById = Object.fromEntries(presets.map((item) => [item.id, item]));
        } catch (error) {
            llmPresetsById = {};
            showToast('Could not load LLM presets', 'error');
        }
        loadKeys();
        const testBtn = document.getElementById('test-llm-btn');
        if (testBtn) testBtn.addEventListener('click', testProviderApi);
        ['llm-key', 'llm-model', 'llm-url'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('input', saveCurrentProvider);
        });
    }

    function saveKeys() {
        saveCurrentProvider();
        persistKeys(getKeys());
        showToast('Settings saved', 'success');
    }

    function getKeys() {
        const saved = JSON.parse(localStorage.getItem('vuza_api_keys') || '{}');
        const valueOf = (id) => {
            const el = document.getElementById(id);
            return el ? el.value.trim() : '';
        };
        saveCurrentProvider();
        const current = {
            llm_key: valueOf('llm-key'),
            llm_url: valueOf('llm-url'),
            llm_model: valueOf('llm-model'),
            llm_preset: valueOf('llm-preset'),
            llm_providers: llmProviders,
            pexels_key: valueOf('pexels-key'),
            pixabay_key: valueOf('pixabay-key'),
            coverr_key: valueOf('coverr-key'),
            piapi_key: valueOf('piapi-key'),
            piapi_model: valueOf('piapi-model') || 'hailuo-2.3-fast',
            yt_client_id: valueOf('yt-client-id'),
            yt_client_secret: valueOf('yt-client-secret'),
            eleven_key: valueOf('eleven-key')
        };
        const merged = { ...saved };
        const stale = ["seedream_key", "seedream_url", "seedream_model", "replicate_key", "replicate_model"];
        stale.forEach((key) => { delete merged[key]; });
        Object.entries(current).forEach(([key, value]) => {
            if (key === 'llm_providers' || key === 'llm_preset' || value) merged[key] = value;
        });
        return merged;
    }

    function persistKeys(keys) {
        const clean = { ...keys };
        const stale = ["seedream_key", "seedream_url", "seedream_model", "replicate_key", "replicate_model"];
        stale.forEach((key) => { delete clean[key]; });
        localStorage.setItem('vuza_api_keys', JSON.stringify(clean));
    }

    async function readErrorMessage(response, fallback) {
        try {
            const err = await response.json();
            const message = err.detail || err.message;
            if (typeof message === 'string') return message;
            if (message) return JSON.stringify(message);
            return fallback;
        } catch (error) {
            return fallback;
        }
    }

    function showApiSettings() {
        if (settingsBody && settingsBody.classList.contains('hidden')) {
            settingsBody.classList.remove('hidden');
            settingsPanel.classList.add('open');
        }
    }

    function numVal(id, fallback) {
        const el = document.getElementById(id);
        const n = el ? Number(el.value) : fallback;
        return Number.isFinite(n) ? n : fallback;
    }

    initLlmProviders();

    const saveBtn = document.getElementById('save-keys-btn');
    if (saveBtn) saveBtn.addEventListener('click', saveKeys);

    if (llmPreset) {
        llmPreset.addEventListener('change', () => {
            saveCurrentProvider();
            loadProvider(currentProviderId());
            persistKeys(getKeys());
        });
    }

    async function refreshMusicOptions() {
        if (!musicSelect) return;
        const current = musicSelect.value;
        try {
            const response = await fetch('/api/music');
            const data = await response.json();
            const files = data.files || ['none'];
            allowedMusic = new Set(files);
            musicSelect.innerHTML = files.map(name => {
                const label = name === 'none' ? 'No music' : name;
                return `<option value="${name}">${label}</option>`;
            }).join('');
            musicSelect.value = files.includes(current) ? current : 'none';
        } catch (error) {
            allowedMusic = new Set(['none', 'cinematic.mp3']);
        }
    }

    if (!tabSingle || !tabScript) return;

    function switchMode(mode) {
        currentMode = mode;
        if (mode === 'single') {
            tabSingle.classList.add('active');
            tabScript.classList.remove('active');
            singleArea.classList.remove('hidden');
            scriptArea.classList.add('hidden');
        } else {
            tabSingle.classList.remove('active');
            tabScript.classList.add('active');
            singleArea.classList.add('hidden');
            scriptArea.classList.remove('hidden');
        }
        updatePrimaryButtonText();
    }

    tabSingle.addEventListener('click', () => switchMode('single'));
    tabScript.addEventListener('click', () => switchMode('script'));

    if (addScriptBtn) {
        addScriptBtn.addEventListener('click', () => {
            const div = document.createElement('div');
            div.className = 'script-item';
            div.innerHTML = `<textarea class="script-input" placeholder="Paste another script for batch generation"></textarea><button type="button" class="remove-script-btn">×</button>`;
            scriptsContainer.appendChild(div);
            div.querySelector('.remove-script-btn').addEventListener('click', () => div.remove());
        });
    }

    if (templateSelect) {
        templateSelect.addEventListener('change', () => {
            const template = templateSelect.value;
            const firstScript = scriptsContainer.querySelector('.script-input');
            if (!firstScript) return;

            if (template === 'suspense_cn') {
                firstScript.value = "凌晨两点，我收到一条陌生短信。\n短信里只有五个字：别回头看。\n可我明明一个人住在这间屋子。\n窗外的雨声突然停了。\n门缝下面，慢慢塞进来一张旧照片。\n照片上站着的，竟然是十年前的我。\n更奇怪的是，我身后还有一个模糊的人影。\n下一秒，手机又响了：他已经进来了。";
                document.getElementById('vibe-suspense').checked = true;
                applySuspenseDefaults();
            } else if (template === 'motivational') {
                firstScript.value = "真正拉开差距的，从来不是某一次爆发。\n而是你在没人看见的时候，依然愿意往前走。\n今天慢一点没关系，只要别停下来。\n你以为自己只是撑过了一天，其实你正在变强。";
                document.getElementById('vibe-aesthetic').checked = true;
                document.getElementById('ratio-9-16').checked = true;
            } else if (template === 'educational') {
                firstScript.value = "你知道吗，蜂蜜几乎不会自然变质。\n考古学家曾在古埃及墓葬里发现三千多年前的蜂蜜。\n它依然可以食用。\n原因是蜂蜜含水量低、酸性强，细菌很难在里面生长。";
                document.getElementById('vibe-general').checked = true;
                document.getElementById('ratio-16-9').checked = true;
            } else if (template === 'storytelling') {
                firstScript.value = "那家旧书店只在雨夜开门。\n小女孩在最里面的书架上，发现了一本没有书名的地图册。\n她刚翻开第一页，柜台上的钟就停了。\n地图中央，慢慢浮现出她家的地址。";
                document.getElementById('vibe-aesthetic').checked = true;
                document.getElementById('ratio-9-16').checked = true;
            } else if (template === 'lofi_vibes') {
                firstScript.value = "深夜的雨敲在窗户上。\n桌上还有一杯温热的咖啡。\n远处的城市灯光慢慢散开。\n这一刻，世界终于安静下来。";
                document.getElementById('vibe-lofi').checked = true;
                document.getElementById('ratio-9-16').checked = true;
            } else if (template === 'news') {
                firstScript.value = "最新消息，科学家发现了一颗可能适合生命存在的类地行星。\n它距离地球约二十光年，围绕一颗红矮星运行。\n研究团队正在进一步确认那里是否存在水和大气。\n这项发现可能会改写我们对宜居星球的认识。";
                document.getElementById('vibe-general').checked = true;
                document.getElementById('ratio-16-9').checked = true;
                document.getElementById('subtitle-style').value = 'yellow_box';
            } else if (template === 'tutorial') {
                firstScript.value = "三步做出一杯更稳定的手冲咖啡。\n第一步，把咖啡豆磨到中细研磨。\n第二步，把水温控制在九十二到九十五度。\n第三步，绕圈慢慢注水，让香气充分释放。";
                document.getElementById('vibe-general').checked = true;
                document.getElementById('ratio-9-16').checked = true;
                document.getElementById('subtitle-style').value = 'bold_outline';
            }
            if (template) showToast('Template loaded', 'success');
        });
    }

    const languageSelect = document.getElementById('language-select');
    const voiceSelect = document.getElementById('voice-select');

    const voiceMap = {
        'en-US': [
            { name: 'Christopher (free)', value: 'en-US-ChristopherNeural' },
            { name: 'Jenny (free)', value: 'en-US-JennyNeural' },
            { name: 'Adam (ElevenLabs)', value: 'eleven_pNInz6obpg8ndclQU7Nc' },
            { name: 'Antoni (ElevenLabs)', value: 'eleven_ErXwBPLxhSj618Y4yxKI' },
            { name: 'Bella (ElevenLabs)', value: 'eleven_EXAVITQu4vr4xnSDxMaL' }
        ],
        'en-GB': [
            { name: 'Ryan', value: 'en-GB-RyanNeural' },
            { name: 'Sonia', value: 'en-GB-SoniaNeural' },
            { name: 'Libby', value: 'en-GB-LibbyNeural' },
            { name: 'Thomas', value: 'en-GB-ThomasNeural' }
        ],
        'es-ES': [
            { name: 'Alvaro', value: 'es-ES-AlvaroNeural' },
            { name: 'Elvira', value: 'es-ES-ElviraNeural' }
        ],
        'fr-FR': [
            { name: 'Henri', value: 'fr-FR-HenriNeural' },
            { name: 'Denise', value: 'fr-FR-DeniseNeural' }
        ],
        'de-DE': [
            { name: 'Conrad', value: 'de-DE-ConradNeural' },
            { name: 'Katja', value: 'de-DE-KatjaNeural' }
        ],
        'it-IT': [
            { name: 'Diego', value: 'it-IT-DiegoNeural' },
            { name: 'Elsa', value: 'it-IT-ElsaNeural' }
        ],
        'hi-IN': [
            { name: 'Madhur', value: 'hi-IN-MadhurNeural' },
            { name: 'Swara', value: 'hi-IN-SwaraNeural' }
        ],
        'ur-PK': [
            { name: 'Asad', value: 'ur-PK-AsadNeural' },
            { name: 'Uzma', value: 'ur-PK-UzmaNeural' }
        ],
        'zh-CN': [
            { name: 'Yunyang (male)', value: 'zh-CN-YunyangNeural' },
            { name: 'Xiaoxiao (female)', value: 'zh-CN-XiaoxiaoNeural' }
        ],
        'ja-JP': [
            { name: 'Keita', value: 'ja-JP-KeitaNeural' },
            { name: 'Nanami', value: 'ja-JP-NanamiNeural' }
        ]
    };

    function updateVoices() {
        const lang = languageSelect.value;
        const voices = voiceMap[lang] || [];
        voiceSelect.innerHTML = voices.map(v => `<option value="${v.value}">${v.name}</option>`).join('') + '<option value="none">No voice (assets only)</option>';
    }

    function applySuspenseDefaults() {
        const setChecked = (id) => {
            const el = document.getElementById(id);
            if (el) el.checked = true;
        };

        setChecked('src-pinterest');
        setChecked('type-photo');
        setChecked('ratio-9-16');
        setChecked('emoji-subs-off');
        setChecked('vibe-suspense');

        if (languageSelect) {
            languageSelect.value = 'zh-CN';
            updateVoices();
        }
        if (voiceSelect) voiceSelect.value = 'zh-CN-YunyangNeural';

        if (musicSelect) musicSelect.value = 'none';

        const subtitleStyle = document.getElementById('subtitle-style');
        if (subtitleStyle) subtitleStyle.value = 'high_retention';
    }

    if (languageSelect) {
        languageSelect.addEventListener('change', updateVoices);
        updateVoices();
    }

    switchMode('script');
    refreshMusicOptions();
    resumeCurrentJob();

    document.querySelectorAll('input[name="source"], input[name="auto_video"]').forEach(input => {
        input.addEventListener('change', () => {
            const source = document.querySelector('input[name="source"]:checked')?.value;
            if (source === 'piapi') {
                const videoType = document.getElementById('type-video');
                if (videoType) videoType.checked = true;
            }
            updateProviderFallbackVisibility();
            updatePrimaryButtonText();
        });
    });

    function updateProviderFallbackVisibility() {
        const wrap = document.getElementById('provider-fallback-wrap');
        if (!wrap) return;
        const source = document.querySelector('input[name="source"]:checked')?.value;
        const stock = source === 'pinterest' || source === 'pexels' || source === 'pixabay' || source === 'coverr';
        wrap.style.display = stock ? '' : 'none';
    }
    updateProviderFallbackVisibility();

    if (scrapeUrlBtn) {
        scrapeUrlBtn.addEventListener('click', async () => {
            const url = urlInput.value.trim();
            if (!url) { showToast('Paste an article URL first', 'error'); return; }

            const keys = getKeys();
            scrapeUrlBtn.disabled = true;
            scrapeUrlBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Extracting...';

            try {
                const response = await fetch('/api/scrape_url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        url: url,
                        api_keys: keys
                    })
                });

                if (response.ok) {
                    const data = await response.json();
                    const firstScript = scriptsContainer.querySelector('.script-input');
                    if (firstScript) {
                        firstScript.value = data.script;
                        showToast('URL summarized into a script', 'success');
                    }
                } else {
                    showToast(await readErrorMessage(response, 'Extract failed'), 'error');
                }
            } catch (error) {
                showToast('Network error', 'error');
            } finally {
                scrapeUrlBtn.disabled = false;
                scrapeUrlBtn.innerHTML = '<i class="fas fa-file-download"></i> Extract script';
            }
        });
    }

    function setKeywords(list) {
        if (keywordsInput) keywordsInput.value = (list || []).join('\n');
    }

    function readKeywords() {
        return (keywordsInput?.value || '').split('\n').map((line) => line.trim()).filter(Boolean);
    }

    function llmKeyPayload() {
        const keys = getKeys();
        return {
            llm_key: keys.llm_key || '',
            llm_url: keys.llm_url || 'https://openrouter.ai/api/v1/chat/completions',
            llm_model: keys.llm_model || ''
        };
    }

    if (generateScriptBtn) {
        generateScriptBtn.addEventListener('click', async () => {
            const topic = topicInput.value.trim();
            if (!topic) { showToast('Enter a topic or paste source text first', 'error'); return; }

            const keys = getKeys();
            const vibe = document.querySelector('input[name="vibe"]:checked').value;

            if (!keys.llm_key) {
                showApiSettings();
                showToast('Add an AI text API key in API settings', 'error');
                return;
            }
            persistKeys(keys);

            generateScriptBtn.disabled = true;
            generateScriptBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating...';

            try {
                const response = await fetch('/api/generate_script', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        topic: topic,
                        vibe: vibe,
                        language: document.getElementById('language-select').value,
                        count: parseInt(countInput?.value || '3', 10) || 3,
                        clip_duration: numVal('clip-duration', 5),
                        api_keys: llmKeyPayload()
                    })
                });

                if (response.ok) {
                    const data = await response.json();
                    const firstScript = scriptsContainer.querySelector('.script-input') || scriptInput;
                    if (firstScript) {
                        firstScript.value = data.script;
                    }
                    if (Array.isArray(data.keywords)) {
                        setKeywords(data.keywords);
                    }
                    showToast('Script and keywords generated', 'success');
                } else {
                    showToast(await readErrorMessage(response, 'Script generation failed'), 'error');
                }
            } catch (error) {
                showToast('Network error', 'error');
            } finally {
                generateScriptBtn.disabled = false;
                generateScriptBtn.innerHTML = '<i class="fas fa-magic"></i> Generate script';
            }
        });
    }

    if (regenerateKeywordsBtn) {
        regenerateKeywordsBtn.addEventListener('click', async () => {
            const topic = (topicInput?.value || '').trim();
            const script = ((scriptsContainer?.querySelector('.script-input') || scriptInput)?.value || '').trim();
            if (!topic) { showToast('Enter a video topic first', 'error'); return; }
            if (!script) { showToast('Generate or paste a narration script first', 'error'); return; }
            const keys = getKeys();
            if (!keys.llm_key) {
                showApiSettings();
                showToast('Add an AI text API key in API settings', 'error');
                return;
            }
            persistKeys(keys);
            regenerateKeywordsBtn.disabled = true;
            regenerateKeywordsBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Regenerating...';
            try {
                const response = await fetch('/api/generate_keywords', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        topic,
                        script,
                        vibe: document.querySelector('input[name="vibe"]:checked')?.value || 'aesthetic',
                        language: document.getElementById('language-select')?.value || 'en-US',
                        count: parseInt(countInput?.value || '3', 10) || 3,
                        clip_duration: numVal('clip-duration', 5),
                        api_keys: llmKeyPayload()
                    })
                });
                if (response.ok) {
                    const data = await response.json();
                    setKeywords(data.keywords || []);
                    showToast('Keywords updated', 'success');
                } else {
                    showToast(await readErrorMessage(response, 'Keyword generation failed'), 'error');
                }
            } catch (error) {
                showToast('Network error', 'error');
            } finally {
                regenerateKeywordsBtn.disabled = false;
                regenerateKeywordsBtn.innerHTML = '<i class="fas fa-sync-alt"></i> Regenerate keywords';
            }
        });
    }

    if (analyzeBtn) {
        analyzeBtn.addEventListener('click', async () => {
            const scripts = Array.from(document.querySelectorAll('.script-input'))
                                .map(s => s.value.trim())
                                .filter(s => s !== "");

            if (scripts.length === 0) { showToast('Enter or generate a script first', 'error'); return; }

            const keys = getKeys();
            if (!keys.llm_key) {
                showApiSettings();
                showToast('Add an AI text API key in API settings', 'error');
                return;
            }
            persistKeys(keys);
            analyzeBtn.disabled = true;
            analyzeBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Analyzing...';

            try {
                const response = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        script: scripts[0],
                        api_keys: {
                            llm_key: keys.llm_key || '',
                            llm_url: keys.llm_url || 'https://openrouter.ai/api/v1/chat/completions',
                            llm_model: keys.llm_model || ''
                        }
                    })
                });

                if (response.ok) {
                    const data = await response.json();
                    aiTitle.value = data.title;
                    aiDesc.value = data.description;
                    aiHashtags.value = data.hashtags;
                    if (aiThumbPrompt) aiThumbPrompt.value = data.thumbnail_prompt || "";
                    analysisPanel.classList.remove('hidden');
                    showToast('Analysis complete', 'success');
                } else {
                    showToast(await readErrorMessage(response, 'Analysis failed'), 'error');
                }
            } catch (error) {
                showToast('Network error', 'error');
            } finally {
                analyzeBtn.disabled = false;
                analyzeBtn.innerHTML = '<i class="fas fa-brain"></i> AI title analysis';
            }
        });
    }

    if (voicePreviewBtn) {
        voicePreviewBtn.addEventListener('click', async () => {
            const voice = document.getElementById('voice-select').value;
            if (voice === 'none') {
                showToast('Choose a TTS voice to preview', 'error');
                return;
            }
            const keys = getKeys();
            voicePreviewBtn.disabled = true;
            try {
                const response = await fetch('/api/tts/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text: 'This is a VUZA voice preview.',
                        voice,
                        language: document.getElementById('language-select').value,
                        voice_rate: numVal('voice-rate', 100) / 100,
                        voice_volume: numVal('voice-volume', 100) / 100,
                        eleven_key: keys.eleven_key || ''
                    })
                });
                if (!response.ok) {
                    showToast(await readErrorMessage(response, 'Voice preview failed'), 'error');
                    return;
                }
                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const audio = new Audio(url);
                audio.onended = () => URL.revokeObjectURL(url);
                await audio.play();
            } catch (error) {
                showToast('Voice preview failed', 'error');
            } finally {
                voicePreviewBtn.disabled = false;
            }
        });
    }

    async function uploadLocalFiles() {
        const input = document.getElementById('local-files');
        if (!input || !input.files || input.files.length === 0) return [];
        const names = [];
        for (const file of input.files) {
            const body = new FormData();
            body.append('file', file);
            const response = await fetch('/api/upload/material', { method: 'POST', body });
            if (!response.ok) {
                throw new Error(await readErrorMessage(response, 'Upload failed'));
            }
            const data = await response.json();
            names.push(data.path);
        }
        return names;
    }

    scrapeBtn.addEventListener('click', async () => {
        const query = (topicInput && topicInput.value.trim()) || (queryInput ? queryInput.value.trim() : "");

        const scripts = Array.from(document.querySelectorAll('.script-input'))
                            .map(s => s.value.trim())
                            .filter(s => s !== "");

        const source = document.querySelector('input[name="source"]:checked').value;
        const mediaType = document.querySelector('input[name="media_type"]:checked').value;
        const vibe = document.querySelector('input[name="vibe"]:checked').value;
        const count = parseInt(countInput.value);

        const ratio = document.querySelector('input[name="ratio"]:checked').value;
        const language = document.getElementById('language-select').value;
        const voice = document.getElementById('voice-select').value;
        const music = document.getElementById('music-select').value;
        const filter = document.getElementById('filter-select').value;
        const subtitleStyle = document.getElementById('subtitle-style').value;
        const subtitles = document.querySelector('input[name="subtitles"]:checked').value === 'true';
        const autoVideo = document.querySelector('input[name="auto_video"]:checked').value === 'true';
        const ytUpload = document.querySelector('input[name="yt_upload"]:checked').value === 'true';
        const emojiSubtitles = document.querySelector('input[name="emoji_subtitles"]:checked').value === 'true';
        const watermark = document.querySelector('input[name="watermark"]:checked').value === 'true';
        const publishConfirmed = document.getElementById('publish-confirm')?.checked === true;
        const providerFallback = document.querySelector('input[name="provider_fallback"]:checked')?.value === 'true';

        if (currentMode === 'single' && source !== 'local' && !query) { showToast('Enter a stock search term', 'error'); return; }
        if (currentMode === 'script' && scripts.length === 0) { showToast('Enter at least one script', 'error'); return; }

        const keys = getKeys();

        if (!allowedMusic.has(music)) {
            showToast('Background music option is invalid. Choose again.', 'error');
            return;
        }

        if (autoVideo && voice === 'none') {
            showToast('Auto video needs a TTS voice. Turn off auto video for asset-only mode.', 'error');
            return;
        }

        if (autoVideo && currentMode === 'single' && source !== 'local') {
            showToast('Single stock search does not assemble a video. Switch to script mode, or turn off auto video.', 'error');
            return;
        }

        if (ytUpload && !publishConfirmed) {
            showToast('YouTube publishing requires the confirmation checkbox.', 'error');
            return;
        }

        if (source === 'piapi') {
            const piapiKey = (keys.piapi_key || '').trim();
            if (!piapiKey || piapiKey.startsWith('r8_')) {
                showApiSettings();
                showToast('Add a PiAPI key from https://app.piapi.ai/ (not a Replicate r8_ token)', 'error');
                return;
            }
        }

        let piapiConfirmed = false;
        if (source === 'piapi') {
            piapiConfirmed = window.confirm(
                'PiAPI is a paid service. VUZA will create one Hailuo task for every detected script scene. Continue with this paid generation?'
            );
            if (!piapiConfirmed) {
                showToast('PiAPI generation cancelled before any paid task was created', 'error');
                return;
            }
        }

        if (currentMode === 'script' && source !== 'local' && !keys.llm_key) {
            showApiSettings();
            showToast('Script mode with stock sources needs an AI text API key for scene keywords.', 'error');
            return;
        }

        setLoading(true);
        finalVideoUrl = '';
        candidateVideos = [];
        pollConnectionErrorShown = false;
        galleryContainer.innerHTML = '<div class="empty-state"><i class="fas fa-spinner fa-spin"></i><p>VUZA is working...</p></div>';

        try {
            let localFiles = [];
            if (source === 'local') {
                localFiles = await uploadLocalFiles();
                if (localFiles.length === 0) {
                    showToast('Choose at least one local file, or pick another source.', 'error');
                    setLoading(false);
                    return;
                }
            }

            persistKeys(keys);
            const response = await fetch('/api/scrape', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    query,
                    keywords: readKeywords(),
                    script: scripts[0],
                    scripts: scripts,
                    source,
                    media_type: mediaType, count,
                    mode: currentMode, vibe,
                    provider_fallback: providerFallback,
                    local_files: localFiles,
                    video_settings: {
                        ratio, voice, subtitles, language,
                        subtitle_style: subtitleStyle, music, filter,
                        emoji_subtitles: emojiSubtitles,
                        watermark: watermark,
                        clip_duration: numVal('clip-duration', 5),
                        bgm_volume: numVal('bgm-volume', 20) / 100,
                        voice_volume: numVal('voice-volume', 100) / 100,
                        voice_rate: numVal('voice-rate', 100) / 100,
                        video_count: numVal('video-count', 1),
                        subtitle_position: document.getElementById('subtitle-position')?.value || 'bottom',
                        font_size: numVal('font-size', 60),
                        text_fore_color: document.getElementById('text-fore-color')?.value || '#FFFFFF',
                        stroke_color: document.getElementById('stroke-color')?.value || '#000000',
                        stroke_width: numVal('stroke-width', 1.5),
                        subtitle_background: document.getElementById('subtitle-background')?.value || 'none',
                        transition: document.getElementById('transition-select')?.value || 'fade'
                    },
                    auto_video: autoVideo,
                    piapi_confirmed: piapiConfirmed,
                    yt_upload: ytUpload,
                    publish_confirmed: publishConfirmed,
                    api_keys: {
                        llm_key: keys.llm_key || '',
                        llm_url: keys.llm_url || 'https://openrouter.ai/api/v1/chat/completions',
                        llm_model: keys.llm_model || '',
                        pexels_key: keys.pexels_key || '',
                        pixabay_key: keys.pixabay_key || '',
                        coverr_key: keys.coverr_key || '',
                        piapi_key: keys.piapi_key || '',
                        piapi_model: keys.piapi_model || 'hailuo-2.3-fast',
                        yt_client_id: keys.yt_client_id || '',
                        yt_client_secret: keys.yt_client_secret || '',
                        eleven_key: keys.eleven_key || ''
                    }
                })
            });

            if (response.ok) {
                const data = await response.json();
                activeTaskId = data.task_id || '';
                showToast('Started', 'success');
                startPollingStatus();
            } else {
                showToast(await readErrorMessage(response, 'Could not start'), 'error');
                setLoading(false);
            }
        } catch (error) {
            showToast(error.message || 'Network error', 'error');
            setLoading(false);
        }
    });

    function statusUrl() {
        return activeTaskId ? `/api/status?task_id=${encodeURIComponent(activeTaskId)}` : '/api/status';
    }

    function startPollingStatus() {
        statusCard.classList.remove('hidden');
        if (statusInterval) clearInterval(statusInterval);
        statusInterval = setInterval(async () => {
            try {
                const response = await fetch(statusUrl());
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const status = await response.json();
                renderStatus(status);
                if (status.final_video) {
                    finalVideoUrl = status.final_video;
                }
                candidateVideos = status.candidates || [];
                if ((status.results && status.results.length > 0) || finalVideoUrl || candidateVideos.length) updateGallery(status.results || []);
                if (!status.is_running) {
                    clearInterval(statusInterval);
                    statusInterval = null;
                    setLoading(false);
                    if (isFailureStatus(status)) {
                        showToast(status.error || status.message || 'Generation failed', 'error');
                    } else {
                        showToast('Done', 'success');
                    }
                }
            } catch (err) {
                clearInterval(statusInterval);
                statusInterval = null;
                setLoading(false);
                showServiceConnectionError();
            }
        }, 2000);
    }

    async function resumeCurrentJob() {
        try {
            const response = await fetch('/api/status');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const status = await response.json();
            activeTaskId = status.task_id || '';

            if (status.final_video) {
                finalVideoUrl = status.final_video;
            }
            candidateVideos = status.candidates || [];

            if (status.results && status.results.length > 0) {
                updateGallery(status.results);
            } else if (finalVideoUrl || candidateVideos.length) {
                updateGallery([]);
            }

            if (status.is_running || normalizeProgress(status.progress) > 0 || (status.results && status.results.length > 0)) {
                statusCard.classList.remove('hidden');
                renderStatus(status);
            }

            if (status.is_running) {
                setLoading(true);
                startPollingStatus();
            } else {
                setLoading(false);
            }
        } catch (err) {
            showServiceConnectionError();
        }
    }

    function updateGallery(results) {
        galleryContainer.innerHTML = '';
        renderFinalVideoCard();
        results.forEach(res => {
            const block = document.createElement('div');
            block.className = 'keyword-block';
            let html = `<h3>${res.keyword}</h3>`;
            if (res.sentence) html += `<span class="sentence-text">"${res.sentence}"</span>`;
            html += `<div class="gallery-grid">`;
            (res.files || []).forEach(file => {
                const isVideo = /\.(mp4|mov|webm)$/i.test(file);
                if (isVideo) {
                    html += `<div class="media-card"><video src="${file}" preload="metadata" loop muted onmouseover="this.play()" onmouseout="this.pause()"></video><div class="media-actions"><a href="${file}" download class="icon-btn"><i class="fas fa-download"></i></a><span class="badge">Video</span></div></div>`;
                } else {
                    html += `<div class="media-card"><img src="${file}" loading="lazy"><div class="media-actions"><a href="${file}" download class="icon-btn"><i class="fas fa-download"></i></a><span class="badge">HD</span></div></div>`;
                }
            });
            html += `</div>`;
            block.innerHTML = html;
            galleryContainer.appendChild(block);
        });
    }

    function renderFinalVideoCard() {
        const videos = [];
        if (finalVideoUrl) videos.push(finalVideoUrl);
        candidateVideos.forEach(url => {
            if (url && !videos.includes(url)) videos.push(url);
        });
        videos.forEach((url, idx) => {
            const card = document.createElement('div');
            card.className = 'final-video-card';
            card.innerHTML = `
                <div class="final-video-copy">
                    <span class="final-video-kicker"><i class="fas fa-check-circle"></i> Candidate ${idx + 1}</span>
                    <strong>Download video</strong>
                </div>
                <a class="final-video-btn" href="${url}" download>
                    <i class="fas fa-download"></i> Download
                </a>
            `;
            galleryContainer.appendChild(card);
        });
    }

    clearBtn.addEventListener('click', () => {
        finalVideoUrl = '';
        candidateVideos = [];
        galleryContainer.innerHTML = '<div class="empty-state"><i class="fas fa-cloud-download-alt"></i><p>Gallery cleared.</p></div>';
        statusCard.classList.add('hidden');
    });

    function updatePrimaryButtonText() {
        const btnText = scrapeBtn.querySelector('.btn-text');
        if (!btnText) return;

        const source = document.querySelector('input[name="source"]:checked')?.value;
        const autoVideo = document.querySelector('input[name="auto_video"]:checked')?.value === 'true';

        if (currentMode === 'script') {
            btnText.textContent = autoVideo ? 'Script to video' : 'Generate assets from script';
        } else if (source === 'piapi') {
            btnText.textContent = autoVideo ? 'Generate AI video' : 'Generate AI clips';
        } else if (source === 'local') {
            btnText.textContent = autoVideo ? 'Local files to video' : 'Use local files';
        } else {
            btnText.textContent = 'Search stock';
        }
    }

    function isFailureStatus(status) {
        const message = status?.message || '';
        return status?.status === 'error' || Boolean(status?.error) || message.trim().startsWith('Error:');
    }

    function normalizeProgress(value) {
        const progress = Number(value);
        if (!Number.isFinite(progress)) return 0;
        return Math.min(100, Math.max(0, Math.round(progress)));
    }

    function renderStatus(status) {
        const progress = normalizeProgress(status.progress);
        const failed = isFailureStatus(status);
        statusMsg.textContent = status.error || status.message || (failed ? 'Generation failed' : 'Working...');
        statusCard.classList.toggle('status-error', failed);
        statusPercent.textContent = `${progress}%`;
        progressFill.style.width = `${progress}%`;
    }

    function showServiceConnectionError() {
        statusCard.classList.remove('hidden');
        statusCard.classList.add('status-error');
        statusMsg.textContent = 'Cannot reach the backend. Confirm python app.py is still running.';
        statusPercent.textContent = '0%';
        progressFill.style.width = '0%';
        if (!pollConnectionErrorShown) {
            showToast('Backend connection error. Retry in a moment.', 'error');
            pollConnectionErrorShown = true;
        }
    }

    function setLoading(loading) {
        scrapeBtn.disabled = loading;
        const btnText = scrapeBtn.querySelector('.btn-text');
        const btnLoader = scrapeBtn.querySelector('.btn-loader');
        const btnIcon = scrapeBtn.querySelector('.fa-rocket');
        if (loading) {
            btnText.textContent = 'Working...';
            if (btnLoader) btnLoader.classList.remove('hidden');
            if (btnIcon) btnIcon.classList.add('hidden');
        } else {
            updatePrimaryButtonText();
            if (btnLoader) btnLoader.classList.add('hidden');
            if (btnIcon) btnIcon.classList.remove('hidden');
        }
    }

    function showToast(message, type = 'success') {
        const toast = document.getElementById('toast');
        if (!toast) return;
        toast.textContent = message;
        toast.className = `toast ${type}`;
        toast.classList.remove('hidden');
        setTimeout(() => toast.classList.add('hidden'), 3500);
    }
});
