Notification.requestPermission();

function sendJSONAPICommand(url, iconUrl, operation, args) {

    var xmlhttp = new XMLHttpRequest();
    var body ={ operation : operation, args : args };
    var params = JSON.stringify( body );

    xmlhttp.onreadystatechange = function () {
        if (xmlhttp.readyState !== 4) {
            return;
        }
        var jsonResponse;
        try {
            jsonResponse = JSON.parse(xmlhttp.responseText);
        } catch (e) {
            spawnNotification("Could not parse server response", iconUrl, "Panda Backup");
            return;
        }
        if (jsonResponse.error) {
            // API error envelope: {"error": {"code": "...", "message": "..."}}
            var errorMessage = typeof jsonResponse.error === "object" ? jsonResponse.error.message : jsonResponse.error;
            spawnNotification("ERROR: " + errorMessage, iconUrl, "Panda Backup");
        }
        else if (jsonResponse.hasOwnProperty('message')) {
            spawnNotification(jsonResponse.message, iconUrl, "Panda Backup");
        }
        else {
            spawnNotification("Could not parse server response", iconUrl, "Panda Backup");
        }
    };
    xmlhttp.open("POST", url, true);

    const token = localStorage.getItem("activeUserToken");

    xmlhttp.setRequestHeader("Content-type", "application/json");
    if (token) {
        xmlhttp.setRequestHeader("Authorization", "Bearer " + token);
    }

    xmlhttp.send(params);

}

function spawnNotification(theBody, theIcon, theTitle) {
    var options = {
        body: theBody,
        icon: theIcon
    };
    new Notification(theTitle, options);
}