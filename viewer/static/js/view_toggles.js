function initViewToggles() {
    initInlineCoverToggle();
    initFilePathToggle();
    initRegularViewerToggle();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initViewToggles);
} else {
    initViewToggles();
}

function applyInlineCoverPreference(showInline) {
    const coverContainer = document.getElementById('inline-cover-container');
    const toggleBtn = document.getElementById('btn-toggle-inline-cover');
    const btnText = toggleBtn ? toggleBtn.querySelector('.inline-cover-btn-text') : null;

    const galleryMainCol = document.getElementById('gallery-main-col');
    const archiveMainCol = document.getElementById('archive-main-col');

    if (showInline) {
        document.documentElement.classList.add('show-inline-cover');
        if (coverContainer) {
            coverContainer.classList.remove('d-none');
            coverContainer.classList.add('d-block');
        }
        if (toggleBtn) {
            toggleBtn.classList.remove('btn-outline-light');
            toggleBtn.classList.add('btn-info', 'text-dark', 'active');
            if (btnText) btnText.textContent = 'Hide inline cover';
        }
        if (galleryMainCol) {
            galleryMainCol.classList.remove('col-12');
            galleryMainCol.classList.add('col-lg-8', 'col-xl-9');
        }
        if (archiveMainCol) {
            archiveMainCol.classList.remove('col-12');
            archiveMainCol.classList.add('col-lg-8', 'col-xl-9');
        }
    } else {
        document.documentElement.classList.remove('show-inline-cover');
        if (coverContainer) {
            coverContainer.classList.remove('d-block');
            coverContainer.classList.add('d-none');
        }
        if (toggleBtn) {
            toggleBtn.classList.remove('btn-info', 'text-dark', 'active');
            toggleBtn.classList.add('btn-outline-light');
            if (btnText) btnText.textContent = 'Inline cover';
        }
        if (galleryMainCol) {
            galleryMainCol.classList.remove('col-lg-8', 'col-xl-9');
            galleryMainCol.classList.add('col-12');
        }
        if (archiveMainCol) {
            archiveMainCol.classList.remove('col-lg-8', 'col-xl-9');
            archiveMainCol.classList.add('col-12');
        }
    }
}

function initInlineCoverToggle() {
    const stored = localStorage.getItem('inlineCoverPreview');
    const showInline = stored === 'true'; // default false
    applyInlineCoverPreference(showInline);

    const toggleBtn = document.getElementById('btn-toggle-inline-cover');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', function () {
            const current = localStorage.getItem('inlineCoverPreview') === 'true';
            const next = !current;
            localStorage.setItem('inlineCoverPreview', next ? 'true' : 'false');
            applyInlineCoverPreference(next);
        });
    }
}

function applyFilePathPreference(showPath) {
    const filePathRow = document.getElementById('archive-filepath-row');
    const toggleBtn = document.getElementById('btn-toggle-filepath');
    const btnText = toggleBtn ? toggleBtn.querySelector('.filepath-btn-text') : null;

    if (showPath) {
        if (filePathRow) {
            filePathRow.classList.remove('d-none');
        }
        if (toggleBtn) {
            toggleBtn.classList.remove('btn-outline-light');
            toggleBtn.classList.add('btn-info', 'text-dark', 'active');
            if (btnText) btnText.textContent = 'Hide File Path';
        }
    } else {
        if (filePathRow) {
            filePathRow.classList.add('d-none');
        }
        if (toggleBtn) {
            toggleBtn.classList.remove('btn-info', 'text-dark', 'active');
            toggleBtn.classList.add('btn-outline-light');
            if (btnText) btnText.textContent = 'Show File Path';
        }
    }
}

function initFilePathToggle() {
    const stored = localStorage.getItem('showArchiveFilePath');
    const showPath = stored === 'true'; // default false
    applyFilePathPreference(showPath);

    const toggleBtn = document.getElementById('btn-toggle-filepath');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', function () {
            const current = localStorage.getItem('showArchiveFilePath') === 'true';
            const next = !current;
            localStorage.setItem('showArchiveFilePath', next ? 'true' : 'false');
            applyFilePathPreference(next);
        });
    }
}

function applyRegularViewerPreference(hideViewer) {
    const viewModeCard = document.getElementById('view-mode-card');
    const regularViewerContainer = document.getElementById('regular-viewer-container');
    const toggleBtn = document.getElementById('btn-toggle-regular-viewer');
    const btnText = toggleBtn ? toggleBtn.querySelector('.regular-viewer-btn-text') : null;

    if (hideViewer) {
        document.documentElement.classList.add('hide-regular-viewer');
        if (viewModeCard) {
            viewModeCard.classList.add('d-none');
        }
        if (regularViewerContainer) {
            regularViewerContainer.classList.add('d-none');
        }
        if (toggleBtn) {
            toggleBtn.classList.remove('btn-outline-light');
            toggleBtn.classList.add('btn-info', 'text-dark', 'active');
            if (btnText) btnText.textContent = 'Show Regular Viewer';
        }
    } else {
        document.documentElement.classList.remove('hide-regular-viewer');
        if (viewModeCard) {
            viewModeCard.classList.remove('d-none');
        }
        if (regularViewerContainer) {
            regularViewerContainer.classList.remove('d-none');
        }
        if (toggleBtn) {
            toggleBtn.classList.remove('btn-info', 'text-dark', 'active');
            toggleBtn.classList.add('btn-outline-light');
            if (btnText) btnText.textContent = 'Hide Regular Viewer';
        }
    }
}

function initRegularViewerToggle() {
    const stored = localStorage.getItem('hideRegularViewer');
    const hideViewer = stored === 'true'; // default false (shown)
    applyRegularViewerPreference(hideViewer);

    const toggleBtn = document.getElementById('btn-toggle-regular-viewer');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', function () {
            const current = localStorage.getItem('hideRegularViewer') === 'true';
            const next = !current;
            localStorage.setItem('hideRegularViewer', next ? 'true' : 'false');
            applyRegularViewerPreference(next);
        });
    }
}
