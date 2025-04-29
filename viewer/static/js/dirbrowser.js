/**
 * Directory Browser Module
 * Provides functionality to browse directories and add paths to the command area
 */
const DirBrowser = (function() {
    // DOM element IDs
    const DOM_IDS = {
        FOLDER_LIST: 'id01',
        CURRENT_PATH: 'id03',
        LOADING_INDICATOR: 'id03b',
        COMMANDS_AREA: 'commands',
        ADD_CURRENT_TOP: 'id04',
        ADD_CURRENT_BOTTOM: 'id05'
    };

    // CSS classes
    const CSS_CLASSES = {
        FOLDER_ROW: 'folder-row',
        FOLDER_NAME: 'folder-name',
        FOLDER_ACTION: 'folder-action',
        HEADER: 'header',
        FOLDER: 'id02d',
        FILE: 'id02f',
        CURRENT_FOLDER: 'current-folder',
        ADD_BUTTON: 'btn btn-sm btn-primary'
    };

    /**
     * Initialize the directory browser
     * @param {string} directory - The directory to browse
     */
    function init(directory) {
        const url = buildApiUrl(directory);
        const loadingNode = document.getElementById(DOM_IDS.LOADING_INDICATOR);
        
        // Show loading indicator
        loadingNode.innerHTML = 'Loading...';
        
        // Make API request
        fetchDirectoryData(url, loadingNode);
    }

    /**
     * Build the API URL for directory browsing
     * @param {string} directory - The directory to browse
     * @returns {string} The complete API URL
     */
    function buildApiUrl(directory) {
        const currentUrl = window.location.href;
        const urlParts = currentUrl.split('/');
        const baseUrl = urlParts.slice(0, -2).join('/') + '/dir-browser/';
        
        return directory ? `${baseUrl}?dir=${directory}` : baseUrl;
    }

    /**
     * Fetch directory data from the API
     * @param {string} url - The API URL
     * @param {HTMLElement} loadingNode - The loading indicator element
     */
    function fetchDirectoryData(url, loadingNode) {
        const xhr = new XMLHttpRequest();
        
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4 && xhr.status === 200) {
                const response = JSON.parse(xhr.responseText);
                loadingNode.innerHTML = '';
                displayFolders(response);
            }
        };
        
        xhr.open('GET', url, true);
        xhr.send();
    }

    /**
     * Display folders and files in the UI
     * @param {Array} response - The API response containing folder data
     */
    function displayFolders(response) {
        const [root, folders] = response;
        const list = document.getElementById(DOM_IDS.FOLDER_LIST);
        
        // Clear existing content
        clearList(list);
        removeById(DOM_IDS.ADD_CURRENT_TOP);
        removeById(DOM_IDS.ADD_CURRENT_BOTTOM);

        // Create and append header
        createHeader(list);
        
        // Create and append folder/file rows
        folders.forEach((folder, index) => {
            const row = createFolderRow(folder, root, index);
            list.appendChild(row);
        });

        // Update current path display and add "Add current folder" buttons
        updateCurrentPathDisplay(root, list);
    }

    /**
     * Clear the folder list
     * @param {HTMLElement} list - The list element to clear
     */
    function clearList(list) {
        while (list.firstChild) {
            list.removeChild(list.firstChild);
        }
    }

    /**
     * Create the header row for the folder list
     * @param {HTMLElement} list - The list element to append the header to
     */
    function createHeader(list) {
        const header = document.createElement('div');
        header.className = `${CSS_CLASSES.FOLDER_ROW} ${CSS_CLASSES.HEADER}`;
        
        const nameCell = document.createElement('div');
        nameCell.className = CSS_CLASSES.FOLDER_NAME;
        nameCell.innerHTML = 'Name';
        
        const actionCell = document.createElement('div');
        actionCell.className = CSS_CLASSES.FOLDER_ACTION;
        actionCell.innerHTML = 'Action';
        
        header.appendChild(nameCell);
        header.appendChild(actionCell);
        list.appendChild(header);
    }

    /**
     * Create a row for a folder or file
     * @param {Array} folder - The folder data [name, isFolder]
     * @param {string} root - The root path
     * @param {number} index - The index of the folder in the list
     * @returns {HTMLElement} The created row element
     */
    function createFolderRow(folder, root, index) {
        const [name, isFolder] = folder;
        const fullPath = root !== '.' ? `${root}/${name}` : name;
        
        const row = document.createElement('div');
        row.className = `${CSS_CLASSES.FOLDER_ROW} ${isFolder ? CSS_CLASSES.FOLDER : CSS_CLASSES.FILE}`;
        
        const nameCell = document.createElement('div');
        nameCell.className = CSS_CLASSES.FOLDER_NAME;
        nameCell.innerHTML = name;
        
        const actionCell = document.createElement('div');
        actionCell.className = CSS_CLASSES.FOLDER_ACTION;
        
        // Set up event handlers based on whether it's a folder or file
        if (isFolder) {
            // For folders, clicking the name navigates into the folder
            nameCell.addEventListener('click', () => init(fullPath));
            
            // Add button to add the folder path (except for '..')
            if (name !== '..') {
                const addButton = document.createElement('button');
                addButton.className = CSS_CLASSES.ADD_BUTTON;
                addButton.innerHTML = 'Add';
                addButton.addEventListener('click', (e) => {
                    e.stopPropagation(); // Prevent triggering the folder navigation
                    addPathToArea(fullPath);
                });
                actionCell.appendChild(addButton);
            }
        } else {
            // For files, clicking the name adds the path
            nameCell.addEventListener('click', () => addPathToArea(fullPath));
            actionCell.innerHTML = '-';
        }
        
        row.appendChild(nameCell);
        row.appendChild(actionCell);
        
        return row;
    }

    /**
     * Update the current path display and add "Add current folder" buttons
     * @param {string} root - The root path
     * @param {HTMLElement} list - The list element
     */
    function updateCurrentPathDisplay(root, list) {
        const currentPathElement = document.getElementById(DOM_IDS.CURRENT_PATH);
        
        if (root === '.') {
            currentPathElement.innerHTML = '';
        } else {
            currentPathElement.innerHTML = root;
            
            // Create "Add current folder" buttons
            createAddCurrentFolderButton(root, list, DOM_IDS.ADD_CURRENT_TOP, true);
            createAddCurrentFolderButton(root, list, DOM_IDS.ADD_CURRENT_BOTTOM, false);
        }
    }

    /**
     * Create an "Add current folder" button
     * @param {string} root - The root path
     * @param {HTMLElement} list - The list element
     * @param {string} id - The ID for the button
     * @param {boolean} insertBefore - Whether to insert before or after the list
     */
    function createAddCurrentFolderButton(root, list, id, insertBefore) {
        const button = document.createElement('div');
        button.setAttribute('id', id);
        button.setAttribute('class', CSS_CLASSES.CURRENT_FOLDER);
        button.addEventListener('click', () => addPathToArea(root));
        button.innerHTML = 'Add current folder';
        
        if (insertBefore) {
            list.parentNode.insertBefore(button, list);
        } else {
            list.parentNode.appendChild(button);
        }
    }

    /**
     * Add a path to the commands textarea
     * @param {string} fullPath - The path to add
     */
    function addPathToArea(fullPath) {
        const commandsArea = document.getElementById(DOM_IDS.COMMANDS_AREA);
        
        if (commandsArea.innerHTML) {
            commandsArea.innerHTML += "\n" + fullPath;
        } else {
            commandsArea.innerHTML = fullPath;
        }
    }

    /**
     * Remove an element by ID
     * @param {string} id - The ID of the element to remove
     */
    function removeById(id) {
        const element = document.getElementById(id);
        if (element) {
            element.parentNode.removeChild(element);
        }
    }

    // Public API
    return {
        init: init
    };
})();

// Initialize the directory browser when the page loads
document.addEventListener('DOMContentLoaded', function() {
    // Expose the dirBrowser function globally for backward compatibility
    window.dirBrowser = DirBrowser.init;
});

