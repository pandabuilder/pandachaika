// ==UserScript==
// @name        Panda Backup
// @namespace   Panda Backup
// @description Adds link to galleries for JSON crawling
// @include     http://e-hentai.org/*
// @include     http://exhentai.org/*
// @include     https://e-hentai.org/*
// @include     https://exhentai.org/*
// @version     0.1.9
// @grant       GM_xmlhttpRequest
// ==/UserScript==

insertCrawler();
Notification.requestPermission();

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
    span.onclick = function() {
        modal.style.display = "none";
    };
    // When the user clicks on Cancel, close the modal
    document.getElementById('cancel-modal').onclick = function() {
        modal.style.display = "none";
    };
    // When the user clicks anywhere outside of the modal, close it
    window.onclick = function(event) {
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

function sendWebCrawlerAction ( link, parentLink, action, reason ) {

    var data ={ api_key : '{{ api_key }}',operation : 'webcrawler',args : {link: link, action: action} };

    if(parentLink !== null) {
        data.args.parentLink = parentLink;
    }

    if(reason !== null) {
        data.args.reason = reason;
    }

    GM_xmlhttpRequest( {
        method : 'POST',
        url : '{{ server_url }}',
        data : JSON.stringify( data ),
        headers : {
            'Content-Type' : 'application/json'
        },
        onload: function (response) {
            var jsonResponse = JSON.parse(response.responseText);
            if(jsonResponse.action === 'confirmDeletion') {
                displayModalConfirm(
                    jsonResponse.message,
                    function () { sendWebCrawlerAction(link, parentLink, 'replaceFound', getDownloadReason()) },
                    function () { sendWebCrawlerAction(link, parentLink, 'queueFound', getDownloadReason()) }
                );
            }
            else {
                if (Notification.permission === "granted") {
                    spawnNotification(jsonResponse.message, "{{ img_url }}", "Panda Backup");
                }
            }
        }
    } );

}

function getDownloadReason() {
    return localStorage.getItem('pandabackup-download reason');
}

function modifyDivList(currentDiv) {

    var galleryLink = currentDiv.getElementsByTagName('a')[0].getAttribute('href');

    var div2 =document.createElement('div');
    div2.style.margin ='5px 0px 0px 0px';
    div2.innerHTML ='<img class="n" src="https://ehgt.org/g/t.png" alt="T" title="' +galleryLink +'"/></img>';
    div2.addEventListener('click',function () { sendWebCrawlerAction(galleryLink, null, null, getDownloadReason()); });
    currentDiv.parentNode.appendChild(div2);

}

function modifyDivThumbnail(currentDiv) {

    var galleryLink =currentDiv.getElementsByClassName('gl3t')[0].getElementsByTagName('a')[0].getAttribute('href');
    var rightBot =currentDiv.getElementsByClassName('gl5t')[0].getElementsByClassName('gldown')[0];

    var div2 =document.createElement('div');
    div2.style.float ='right';
    div2.style.margin ='5px -20px 0px 10px';
    div2.innerHTML = '<img class="tn" src="https://ehgt.org/g/t.png" alt="T" title="' + galleryLink + '"/></img>';
    div2.addEventListener('click',function () {
        sendWebCrawlerAction(galleryLink, null, null, getDownloadReason());
    });
    rightBot.parentNode.appendChild(div2);

}

function insertCrawler() {
	//Gallery page
    var container =document.getElementById('gd5');
    if ( container !==null ) {

        //get info nodes, check if parent info exists.
        var infoNodes =document.getElementsByClassName('gdt1');
        var parentLink = null;
        if (infoNodes.length !==0) {
            for(var k =0; k <infoNodes.length; k++) {
                if(infoNodes[k].innerHTML.includes('Parent:') && infoNodes[k].nextSibling.getElementsByTagName('a').length > 0) {
                    parentLink = infoNodes[k].nextSibling.getElementsByTagName('a')[0].href;

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
}');
                    insertModal();

                }
            }
        }
        (function () {
            var galleryLink =document.URL;
            var div2 =document.createElement('div');
            div2.innerHTML ='<img style="height: auto; width: auto" src="https://ehgt.org/g/t.png" alt="T" title="' + galleryLink + '"/></img>';
            div2.addEventListener('click',function () { sendWebCrawlerAction(galleryLink, parentLink, null, getDownloadReason()); });
            container.appendChild(div2);
        }());
        return true;
    }
    //Main page, list mode
    var list =document.getElementsByClassName('gl3m');
    if (list.length !==0) {
        for(var i =0; i <list.length; i++) {
            (modifyDivList(list[i]));
        }
        return true;
    }
    //Main page, thumbnails mode
    var list2 =document.getElementsByClassName('gl1t');
    if(list2.length !==0) {
        for(var j =0; j <list2.length; j++) {
            (modifyDivThumbnail(list2[j]));
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
