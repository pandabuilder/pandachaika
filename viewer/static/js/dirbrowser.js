function dirBrowser(directory) {

    var xmlhttp = new XMLHttpRequest();

    var lo2 = window.location.href;
    var indices = [];
    for(var i=0; i<lo2.length;i++) {
        if (lo2[i] === "/") indices.push(i);
    }
    var url = lo2.substr(0, indices[indices.length - 2] + 1) + 'dir-browser/';
    if (directory)
        url = url + "?dir=" + directory;

    var loadingNode = document.getElementById("id03b");

    xmlhttp.onreadystatechange = function () {
        if (xmlhttp.readyState === 4 && xmlhttp.status === 200) {
            var response = JSON.parse(xmlhttp.responseText);
            loadingNode.innerHTML = '';
            DisplayFolders(response);
        }
    };
    xmlhttp.open("GET", url, true);
    xmlhttp.send();
    loadingNode.innerHTML = 'Loading...';

    function DisplayFolders(response) {
        var i;
        var root = response[0];
        var folders = response[1];
        var list = document.getElementById("id01");
        while (list.firstChild) {
            list.removeChild(list.firstChild);
        }

        removeById("id04");
        removeById("id05");

        for (i = 0; i < folders.length; i++) {
            var liObj = document.createElement('li');
            liObj.innerHTML = folders[i][0];
            (function (i) {
                var fullpath = folders[i][0];
                if (root !== '.')
                    fullpath = root + "/" + fullpath;
                if (folders[i][1] === true) {
                    liObj.setAttribute('class', 'id02d');
                    liObj.addEventListener('click', function () { dirBrowser(fullpath); });
                }
                else {
                    liObj.setAttribute('class', 'id02f');
                    liObj.addEventListener('click', function () { AddPathtoArea(fullpath); });
                }
            }(i));
            list.appendChild(liObj);
        }

        if (root === '.') {
            document.getElementById("id03").innerHTML = '';
        }
        else {
            document.getElementById("id03").innerHTML = root;
            var addCurrent = document.createElement('div');
            addCurrent.setAttribute('id', 'id04');
            addCurrent.setAttribute('class', 'current-folder');
            addCurrent.addEventListener('click', function () { AddPathtoArea(root); });
            addCurrent.innerHTML = 'Add current folder';
            list.parentNode.insertBefore(addCurrent, list);
            var secondAddCurrent = addCurrent.cloneNode(true);
            secondAddCurrent.setAttribute('id', 'id05');
            secondAddCurrent.addEventListener('click', function () { AddPathtoArea(root); });
            list.parentNode.appendChild(secondAddCurrent);
        }
        
    }

    function AddPathtoArea(fullPath) {
        if (document.getElementById("commands").innerHTML) {
            document.getElementById("commands").innerHTML += "\n" + fullPath
        }
        else {
            document.getElementById("commands").innerHTML = fullPath
        }

    }

    function removeById(id) {

        var elem;
        if (document.getElementById(id)) {
            return (elem = document.getElementById(id)).parentNode.removeChild(elem);
        }
        
    }

}

