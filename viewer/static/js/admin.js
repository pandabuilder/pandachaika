Notification.requestPermission();

function sendJSONAPICommand(url, api_key, iconUrl, operation, args) {

    var xmlhttp = new XMLHttpRequest();
    var body ={ api_key : api_key,operation : operation, args : args };
    var params = JSON.stringify( body );

    xmlhttp.onreadystatechange = function () {
        if (xmlhttp.readyState === 4 && xmlhttp.status === 200) {
            var jsonResponse = JSON.parse(xmlhttp.responseText);
            if(jsonResponse.hasOwnProperty('message')) {
                spawnNotification(jsonResponse.message, iconUrl, "Panda Backup");
            }
            else if (jsonResponse.hasOwnProperty('error')) {
                spawnNotification("ERROR: " + jsonResponse.error, iconUrl, "Panda Backup");
            }
            else {
                spawnNotification("Could not parse server response", iconUrl, "Panda Backup");
            }
        }
    };
    xmlhttp.open("POST", url, true);

    xmlhttp.setRequestHeader("Content-type", "application/json");

    xmlhttp.send(params);

}

function spawnNotification(theBody, theIcon, theTitle) {
    var options = {
        body: theBody,
        icon: theIcon
    };
    new Notification(theTitle, options);
}