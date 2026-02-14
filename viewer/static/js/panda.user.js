// ==UserScript==
// @name        Panda Backup
// @namespace   Panda Backup
// @description Adds link to galleries for JSON crawling
// @include     http://e-hentai.org/*
// @include     http://exhentai.org/*
// @include     https://e-hentai.org/*
// @include     https://exhentai.org/*
// @version     0.2.03
// @grant       GM_xmlhttpRequest
// @grant       GM_setValue
// @grant       GM_getValue
// ==/UserScript==


const showConfigModal = () => {
    let modal = document.getElementById('configModal');
    if (!modal) {
        modal = document.createElement('div');
        modal.setAttribute('id', 'configModal');
        modal.setAttribute('class', 'modal');
        modal.innerHTML = '<div class="modal-content"><div class="modal-header"><span id="closeConfigModal" class="close">×</span><h2>Configuration</h2></div><div class="modal-body"><p id="configModalMessage">Message</p></div><div class="modal-footer"><p>Token:</p><input type="text" id="user-token-text" size="60"/><br/><p>Server URL (no trailing slash):</p><input type="text" id="server-url-text" size="60"/><br/><p>Icon URL (for browser notifications):</p><input type="text" id="icon-url-text" size="60"/><br/><p>Download Reason:</p><input type="text" id="download-reason-text" size="60"/><br/><br/><button id="ok-config-modal" class="modal-button">Save</button><button id="cancel-config-modal" class="modal-button">Cancel</button></div></div>';
        document.body.appendChild(modal);
        modal = document.getElementById('configModal');

        // Get the <span> element that closes the modal
        let span = document.getElementById("closeConfigModal");

        document.getElementById('configModalMessage').innerHTML = "Enter the Token, Server URL, Icon URL and Download Reason you want to use for requests to Panda Backup. You can manually reset this values by deleting activeUserToken, activeUserServerUrl, activeUserIconUrl or activeUserDownloadReason from this script's data";

        // When the user clicks on <span> (x), close the modal
        span.onclick = function () {
            modal.style.display = "none";
        };
        // When the user clicks on Cancel, close the modal
        document.getElementById('cancel-config-modal').onclick = function () {
            modal.style.display = "none";
        };
        // When the user clicks anywhere outside the modal, close it
        window.onclick = function (event) {
            if (event.target === modal) {
                modal.style.display = "none";
            }
        };

        document.getElementById('ok-config-modal').onclick = function () {
            const newToken = document.getElementById('user-token-text').value;
            const newUrl = document.getElementById('server-url-text').value;
            const newIconUrl = document.getElementById('icon-url-text').value;
            const newDownloadReason = document.getElementById('download-reason-text').value;
            GM_setValue("activeUserToken", newToken);
            GM_setValue("activeUserServerUrl", newUrl);
            GM_setValue("activeUserIconUrl", newIconUrl);
            GM_setValue("activeUserDownloadReason", newDownloadReason);
            modal.style.display = "none";
        };
    }

    const currentToken = GM_getValue("activeUserToken", null);
    const currentServerUrl = GM_getValue("activeUserServerUrl", null);
    const currentIconUrl = GM_getValue("activeUserIconUrl", null);
    const currentDownloadReason = GM_getValue("activeUserDownloadReason", "backup");

    if (currentToken) {
        document.getElementById('user-token-text').value = currentToken;
    }

    if (currentServerUrl) {
        document.getElementById('server-url-text').value = currentServerUrl;
    }

    if (currentIconUrl) {
        document.getElementById('icon-url-text').value = currentIconUrl;
    }

    if (currentDownloadReason) {
        document.getElementById('download-reason-text').value = currentDownloadReason;
    }

    modal.style.display = "block";
}

const checkConfiguration = () => {
    const currentToken = GM_getValue("activeUserToken", null);
    const currentServerUrl = GM_getValue("activeUserServerUrl", null);

    if (currentToken === null || currentServerUrl === null) {
        showConfigModal();
    }
}

const insertSettingsIcon = () => {
    const iconDiv = document.createElement('div');
    iconDiv.style.position = 'fixed';
    iconDiv.style.top = '10px';
    iconDiv.style.right = '10px';
    iconDiv.style.zIndex = '10000';
    iconDiv.style.cursor = 'pointer';
    iconDiv.style.backgroundColor = 'rgba(255, 255, 255, 0.8)';
    iconDiv.style.padding = '5px';
    iconDiv.style.borderRadius = '5px';
    iconDiv.style.boxShadow = '0 0 5px rgba(0,0,0,0.5)';
    iconDiv.innerHTML = '⚙️';
    iconDiv.title = 'Panda Backup Settings';

    iconDiv.addEventListener('click', function () {
        showConfigModal();
    });

    document.body.appendChild(iconDiv);
}

addGlobalStyle('/* The Modal (background) */\
.modal {\
    display: none; /* Hidden by default */\
    position: fixed; /* Stay in place */\
    z-index: 3; /* Sit on top */\
    padding-top: 100px; /* Location of the box */\
    left: 0;\
    top: 0;\
    width: 100%; /* Full width */\
    height: 100%; /* Full height */\
    overflow: auto; /* Enable scroll if needed */\
    background-color: rgb(0,0,0); /* Fallback color */\
    background-color: rgba(0,0,0,0.4); /* Black w/ opacity */\
}\
\
/* Modal Content */\
.modal-content {\
    position: relative;\
    background-color: #fefefe;\
    margin: auto;\
    padding: 0;\
    border: 1px solid #888;\
    width: 50%;\
    box-shadow: 0 4px 8px 0 rgba(0,0,0,0.2),0 6px 20px 0 rgba(0,0,0,0.19);\
}\
\
\
/* The Close Button */\
.close {\
    color: white;\
    float: right;\
    font-size: 28px;\
    font-weight: bold;\
}\
\
.close:hover,\
.close:focus {\
    color: #000;\
    text-decoration: none;\
    cursor: pointer;\
}\
\
.modal-header {\
    padding: 2px 16px;\
    background-color: #5cb85c;\
    color: white;\
}\
\
.modal-body {padding: 2px 16px;\
color: black;\
}\
.modal-footer {\
    padding: 2px 16px;\
    background-color: #5cb85c;\
    color: white;\
}\
.modal-button {\
    background-color: #4CAF50; /* Green */\
    border: none;\
    color: white;\
    padding: 10px 23px;\
    text-align: center;\
    text-decoration: none;\
    display: inline-block;\
    font-size: 16px;\
}\
/* Click feedback animation */\
@keyframes click-feedback {\
    0%   { transform: scale(1); filter: brightness(100%); }\
    50%  { transform: scale(1.15); filter: brightness(150%); }\
    100% { transform: scale(1); filter: brightness(100%); }\
}\
.clicked-image-feedback {\
    animation: click-feedback 0.3s ease-out;\
}');


insertCrawler();
Notification.requestPermission();

checkConfiguration();
insertSettingsIcon();

function addGlobalStyle(css) {
    var head, style;
    head = document.getElementsByTagName('head')[0];
    if (!head) { return; }
    style = document.createElement('style');
    style.type = 'text/css';
    style.innerHTML = css;
    head.appendChild(style);
}

function insertModal() {
    var modal = document.createElement('div');
    modal.setAttribute('id', 'confirmModal');
    modal.setAttribute('class', 'modal');
    modal.innerHTML = '<div class="modal-content"><div class="modal-header"><span id="closeModal" class="close">×</span><h2>Confirm</h2></div><div class="modal-body"><p id="modalMessage">Message</p></div><div class="modal-footer"> <button id="ok-modal" class="modal-button">OK</button><button id="no-modal" class="modal-button">No</button><button id="cancel-modal" class="modal-button">Cancel</button></div></div>';
    document.body.appendChild(modal);
    modal = document.getElementById('confirmModal');
    // Get the <span> element that closes the modal
    var span = document.getElementById("closeModal");

    // When the user clicks on <span> (x), close the modal
    span.onclick = function () {
        modal.style.display = "none";
    };
    // When the user clicks on Cancel, close the modal
    document.getElementById('cancel-modal').onclick = function () {
        modal.style.display = "none";
    };
    // When the user clicks anywhere outside the modal, close it
    window.onclick = function (event) {
        if (event.target === modal) {
            modal.style.display = "none";
        }
    };
}

function displayModalConfirm(message, cbOK, cbNO) {
    var modal = document.getElementById('confirmModal');
    document.getElementById('modalMessage').innerHTML = message;
    document.getElementById('ok-modal').onclick = function () { cbOK(); modal.style.display = "none"; };
    document.getElementById('no-modal').onclick = function () { cbNO(); modal.style.display = "none"; };
    modal.style.display = "block";

}

function sendWebCrawlerAction(link, parentLink, action, reason) {

    var data = { operation: 'webcrawler', args: { link: link, action: action } };

    if (parentLink !== null) {
        data.args.parentLink = parentLink;
    }

    if (reason !== null) {
        data.args.reason = reason;
    }

    const token = GM_getValue("activeUserToken", '');

    GM_xmlhttpRequest({
        method: 'POST',
        url: GM_getValue("activeUserServerUrl", "") + "/admin-api/",
        data: JSON.stringify(data),
        headers: {
            'Content-Type': 'application/json',
            "Authorization": "Bearer " + token
        },
        onload: function (response) {
            var jsonResponse = JSON.parse(response.responseText);
            if (jsonResponse.action === 'confirmDeletion') {
                displayModalConfirm(
                    jsonResponse.message,
                    function () { sendWebCrawlerAction(link, parentLink, 'replaceFound', getDownloadReason()) },
                    function () { sendWebCrawlerAction(link, parentLink, 'queueFound', getDownloadReason()) }
                );
            }
            else {
                if (Notification.permission === "granted") {
                    spawnNotification(jsonResponse.message, GM_getValue("activeUserIconUrl", ""), "Panda Backup");
                }
            }
        }
    });

}

function getDownloadReason() {
    return GM_getValue("activeUserDownloadReason", "backup");
}

/**
 * Applies a visual feedback animation to the image within the clicked element.
 * @param {HTMLElement} element The element that was clicked.
 */
function addClickFeedback(element) {
    const img = element.getElementsByTagName('img')[0];
    if (img) {
        img.classList.add('clicked-image-feedback');
        // Remove the class after the animation is done (duration is 300ms in CSS)
        setTimeout(() => {
            img.classList.remove('clicked-image-feedback');
        }, 300);
    }
}

function modifyDivList(currentDiv) {
    var galleryLink = currentDiv.getElementsByTagName('a')[0].getAttribute('href');
    var div2 = document.createElement('div');
    div2.style.margin = '5px 0px 0px 0px';
    div2.innerHTML = '<img class="n" src="https://ehgt.org/g/t.png" alt="T" title="' + galleryLink + '"/></img>';
    div2.addEventListener('click', function () {
        addClickFeedback(this);
        sendWebCrawlerAction(galleryLink, null, null, getDownloadReason());
    });
    currentDiv.parentNode.appendChild(div2);
}

function modifyDivThumbnail(currentDiv) {
    var galleryLink = currentDiv.getElementsByClassName('gl3t')[0].getElementsByTagName('a')[0].getAttribute('href');
    var rightBot = currentDiv.getElementsByClassName('gl5t')[0].getElementsByClassName('gldown')[0];

    var div2 = document.createElement('div');
    div2.style.float = 'right';
    div2.style.margin = '5px -20px 0px 10px';
    div2.innerHTML = '<img class="tn" src="https://ehgt.org/g/t.png" alt="T" title="' + galleryLink + '"/></img>';
    div2.addEventListener('click', function () {
        addClickFeedback(this);
        sendWebCrawlerAction(galleryLink, null, null, getDownloadReason());
    });
    rightBot.parentNode.appendChild(div2);
}

function modifyDivExtended(currentDiv) {
    var galleryLink = currentDiv.getElementsByTagName('div')[0].children[1].getAttribute('href');
    var rightBot = currentDiv.getElementsByTagName('div')[0].getElementsByClassName('gl3e')[0].getElementsByClassName('gldown')[0];

    var div2 = document.createElement('div');
    div2.style.margin = '0px 0px 0px 2px';
    div2.innerHTML = '<img class="tn" src="https://ehgt.org/g/t.png" alt="T" title="' + galleryLink + '"/></img>';
    div2.addEventListener('click', function () {
        addClickFeedback(this);
        sendWebCrawlerAction(galleryLink, null, null, getDownloadReason());
    });
    rightBot.parentNode.appendChild(div2);
}

function insertCrawler() {
    //Gallery page
    var container = document.getElementById('gd5');
    if (container !== null) {

        //get info nodes, check if parent info exists.
        var infoNodes = document.getElementsByClassName('gdt1');
        var parentLink = null;
        if (infoNodes.length !== 0) {
            for (var k = 0; k < infoNodes.length; k++) {
                if (infoNodes[k].innerHTML.includes('Parent:') && infoNodes[k].nextSibling.getElementsByTagName('a').length > 0) {
                    parentLink = infoNodes[k].nextSibling.getElementsByTagName('a')[0].href;

                    insertModal();

                }
            }
        }
        (function () {
            var galleryLink = document.URL;
            var div2 = document.createElement('div');
            div2.innerHTML = '<img style="height: auto; width: auto" src="https://ehgt.org/g/t.png" alt="T" title="' + galleryLink + '"/></img>';
            div2.addEventListener('click', function () {
                addClickFeedback(this);
                sendWebCrawlerAction(galleryLink, parentLink, null, getDownloadReason());
            });
            container.appendChild(div2);
        }());
        return true;
    }
    //Main page, list mode
    var list = document.getElementsByClassName('gl3m');
    if (list.length !== 0) {
        for (var i = 0; i < list.length; i++) {
            (modifyDivList(list[i]));
        }
        return true;
    }
    //Main page, thumbnails mode
    var list2 = document.getElementsByClassName('gl1t');
    if (list2.length !== 0) {
        for (var j = 0; j < list2.length; j++) {
            (modifyDivThumbnail(list2[j]));
        }
        return true;
    }
    //Main page, extended mode
    var list3 = document.getElementsByClassName('gl2e');
    if (list3.length !== 0) {
        for (var jj = 0; jj < list3.length; jj++) {
            (modifyDivExtended(list3[jj]));
        }
        return true;
    }

}

function spawnNotification(theBody, theIcon, theTitle) {
    var options = {
        body: theBody,
        icon: theIcon
    };
    new Notification(theTitle, options);
}