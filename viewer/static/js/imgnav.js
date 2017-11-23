(function() {
    window.Bindings = Keys.Bindings;
    window.Combo    = Keys.Combo;
    window.Key      = Keys.Key;

    // Initialize application-wide bindings manager
    window.bindings = new Bindings();

    // Add binding to display an alert
    bindings.add('moveLeft', new Combo(Key.Left), new Combo(Key.A));
    bindings.add('moveRight', new Combo(Key.Right), new Combo(Key.D));
    bindings.add('moveFirst', new Combo(Key.Left, Key.SHIFT));
    bindings.add('moveLast', new Combo(Key.Right, Key.SHIFT));
    bindings.add('toggleFullSize', new Combo(Key.F));
    bindings.add('contentFullScreen', new Combo(Key.F, Key.SHIFT));
    bindings.add('goArchive', new Combo(Key.B, Key.SHIFT));

    bindings.registerHandler('moveLeft', moveLeft);
    bindings.registerHandler('moveRight', moveRight);
    bindings.registerHandler('moveFirst', moveFirst);
    bindings.registerHandler('moveLast', moveLast);
    bindings.registerHandler('toggleFullSize', toggleFullSize);
    bindings.registerHandler('contentFullScreen', contentFullScreen);
    bindings.registerHandler('goArchive', goArchive);

    function goArchive() {
        window.location = back_url
    }

    function toggleFullSize() {
        var centerImg = document.getElementById("cent-img");
        if(centerImg.hasAttribute("display-width")){
            centerImg.removeAttribute("display-width")
        }
        else {
            centerImg.setAttribute("display-width", "")
        }
    }

})();

function moveLeft(ev) {
    if(navImages[0].length > 0)
        goToPosition(navImages[0][0]);
    if(ev){
        ev.preventDefault();
    }
    return false;
}
function moveRight(ev) {
    if(navImages[1].length > 0)
        goToPosition(navImages[1][0]);
    if(ev){
        ev.preventDefault();
    }
    return false;
}

function moveFirst(ev) {
    goToPosition(start_position);
    if(ev){
        ev.preventDefault();
    }
    return false;
}
function moveLast(ev) {
    goToPosition(last_position);
    if(ev){
        ev.preventDefault();
    }
    return false;
}

function contentFullScreen() {

    if (!document.fullscreenElement &&    // alternative standard method
        !document.mozFullScreenElement && !document.webkitFullscreenElement && !document.msFullscreenElement ) {
        var elem = document.getElementById("img-container");
        if (elem.requestFullscreen) {
            elem.requestFullscreen();
        } else if (elem.msRequestFullscreen) {
            elem.msRequestFullscreen();
        } else if (elem.mozRequestFullScreen) {
            elem.mozRequestFullScreen();
        } else if (elem.webkitRequestFullscreen) {
            elem.webkitRequestFullscreen();
        }
      }
    else {
        if (document.exitFullscreen) {
            document.exitFullscreen();
        } else if (document.msExitFullscreen) {
            document.msExitFullscreen();
        } else if (document.mozCancelFullScreen) {
            document.mozCancelFullScreen();
        } else if (document.webkitExitFullscreen) {
            document.webkitExitFullscreen();
        }
  }
}

function urlPost(url, params) {

    var xmlhttp = new XMLHttpRequest();
    xmlhttp.open("POST", url);
    xmlhttp.setRequestHeader("Content-Type", "application/json");
    xmlhttp.setRequestHeader("X-CSRFToken", csrftoken);
    xmlhttp.onreadystatechange = function () {
        if (xmlhttp.readyState === 4 && xmlhttp.status === 200) {
            location.reload();
            //var response = JSON.parse(xmlhttp.responseText);
            //alert("sent command")
        }
    };
    //xmlhttp.open("POST", url, true);
    xmlhttp.send(JSON.stringify(params));

}

serialize = function(obj, prefix) {
  var str = [];
  for(var p in obj) {
    if (obj.hasOwnProperty(p)) {
      var k = prefix ? prefix : p, v = obj[p];
      str.push(typeof v === "object" ?
        serialize(v, k) :
        encodeURIComponent(k) + "=" + encodeURIComponent(v));
    }
  }
  return str.join("&");
};

function setOptions() {
    var imageVerticalWidth = document.getElementById("image-vertical-width").value;
    var imageHorizontalWidth = document.getElementById("image-horizontal-width").value;
    var params = {
        'viewer_parameters': true,
        'image_width_vertical': imageVerticalWidth,
        'image_width_horizontal': imageHorizontalWidth
    };
    urlPost(base_url + '/session-settings/', params);
}

function getCookie(cname) {
    var name = cname + "=";
    var ca = document.cookie.split(';');
    for (var i = 0; i < ca.length; i++) {
        var c = ca[i];
        while (c.charAt(0) === ' ') c = c.substring(1);
        if (c.indexOf(name) === 0) return c.substring(name.length, c.length);
    }
    return "";
}

var touchpos = {x:0, y:0, t:0};

function touchstart(evt){
	var touches = evt.touches;
	if (touches && touches.length) {
		touchpos = {
			x: touches[0].pageX,
			y: touches[0].pageY,
			t: new Date().getTime()
		};
	}
}

function touchend(evt){
	var touches = evt.touches;
	if (touches && touches.length) {
		var dx = touches[0].pageX - touchpos.x;
		var dy = touches[0].pageY - touchpos.y;
		var dt = new Date().getTime() - touchpos.t;

		if(dt < 500 && Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 50){
			if(dx > 0) moveLeft();
			else moveRight();
		}
	}
}

function imgClick(evt){
	return imgRight(evt) ? moveRight() : moveLeft();
}

function imgRight(evt){
	var img = evt.currentTarget;
	var left = 0;
	var offset = img;
	while(offset){
		left += offset.offsetLeft;
		offset = offset.offsetParent;
	}
	if(img.style.paddingLeft) left += Number(img.style.paddingLeft.match(/\d+/)[0]);
	left -= window.scrollX;
	var x = evt.clientX - left;
	var w = img.width || img.offsetWidth;
	return x/w>0.5;
}

function getNonCachedImagesUrls(archive, positions, current_pos, change) {
    var changed = false;
    if(change && images.hasOwnProperty(current_pos)){
        changeImage(images[current_pos]);
        changeBody(current_pos);
        changed = true;
    }
    positions = positions.filter(function (id){return !images.hasOwnProperty(id)});
    var a = {
        i: positions
    };
    if(positions.length !== 0) {
        var xmlhttp = new XMLHttpRequest();
        xmlhttp.open("GET", base_url + "/r-api/archive/" + archive + "/image_list/?" + serialize(a));
        xmlhttp.setRequestHeader("Content-Type", "application/json");
        // xmlhttp.setRequestHeader("X-CSRFToken", csrftoken);
        xmlhttp.onreadystatechange = function () {
            if (xmlhttp.readyState !== XMLHttpRequest.DONE) {
                return;
            }
            if (xmlhttp.status !== 200) {
                return;
            }
            var image_list = JSON.parse(xmlhttp.responseText);
            if(change){
                getImagesData(image_list, current_pos, change);
                if(!changed) {
                    changeImage(images[current_pos]);
                    changeBody(current_pos);
                }
            }
            else {
                getImagesData(image_list, current_pos, change);
            }
        };
        xmlhttp.send();
    }
}

function getImagesData(image_list, current_pos, change) {
    image_list.forEach(function process(img) {
        if (!images.hasOwnProperty(img.position)) {
            images[img.position] = new Image();
            images[img.position].setAttribute("class", "img-responsive center-block");
            if(img['is_horizontal']) {
                images[img.position].setAttribute("is-horizontal", "");
                if(img['width'] >= image_width_horizontal){
                    images[img.position].width = image_width_horizontal;
                }
                else {
                    images[img.position].width = img['width'];
                }
            }
            else {
                if(img['width'] >= image_width_vertical){
                    images[img.position].width = image_width_vertical;
                }
                else {
                    images[img.position].width = img['width'];
                }
            }
            images[img.position].src = img['url'];
            images[img.position].addEventListener('click', imgClick);
            imageContainer.appendChild(images[img.position]);
            if(change && img.position === current_pos){
                changeImage(images[current_pos]);
            }
        }
    }
    )
}

function changeImage(image) {
    var current_image = document.getElementById("cent-img");
    current_image.removeAttribute("id");
    image.setAttribute("id", "cent-img");
    window.scrollTo(0, image.offsetTop);
}

function getRelativeImages(current, start, end, length) {
    var start_range = current - length;
    var end_range = current + length;
    if(start_range < start)
        start_range = start;
    if (end_range > end)
        end_range = end;
    return [
        Array.apply(null, new Array(current - start_range)).map(function (_, i) {return current-i-1;}),
        Array.apply(null, new Array(end_range - current)).map(function (_, i) {return i+current+1;})
    ];

}

function changeBody(position){
   var stepLinks = document.getElementsByClassName("step-links-inner");
    if(stepLinks.length === 0)
        return;
    for(var i = 0; i < stepLinks.length; i++) {
        stepLinks[i].selectedIndex = position - 1;
    }
    if(position < last_position) {
        document.getElementsByClassName("step-links-left")[0].href = archive_url + (position - 1) + "/#cent-img";
        document.getElementsByClassName("step-links-left")[1].href = archive_url + (position - 1) + "/#cent-img";
    }
    else {
        document.getElementsByClassName("step-links-left")[0].href = '';
        document.getElementsByClassName("step-links-left")[1].href = '';
    }
    if(position > start_position) {
        document.getElementsByClassName("step-links-right")[0].href = archive_url + (position + 1) + "/#cent-img";
        document.getElementsByClassName("step-links-right")[1].href = archive_url + (position + 1) + "/#cent-img";
    }
    else {
        document.getElementsByClassName("step-links-right")[0].href = '';
        document.getElementsByClassName("step-links-right")[1].href = '';
    }
    document.getElementById("img-counter").innerHTML = position;

}

function goToPosition(position) {
    var change = true;
    if(images.hasOwnProperty(position)){
        changeImage(images[position]);
        navImages = getRelativeImages(position, start_position, last_position, 5);
        changeBody(position);
        change = false;
    }
    else {
        navImages = getRelativeImages(position, start_position, last_position, 5);
    }
    getNonCachedImagesUrls(archive, navImages[1].concat(navImages[0]).concat(position), position, change);
    if(images.hasOwnProperty(position)) {
        history.replaceState({
            page: position
        }, document.title, archive_url + position + "/#cent-img");
    }
}

function addLoadEvent(func) {
	var oldonload = window.onload;
	if (typeof window.onload !== 'function') {
		window.onload = func;
	} else {
		window.onload = function() {
			if (oldonload) {
				oldonload();
			}
			func();
		}
	}
}

function clickOptionHandler(i) {
    return function () {
        goToPosition(i.value);
    }
}

function addListenersToNavigators(){
   var stepLinks = document.getElementsByClassName("step-links-inner");
    if(stepLinks.length === 0)
        return;
    for(var i = 0; i < stepLinks.length; i++) {
        stepLinks[i].addEventListener('change', clickOptionHandler(stepLinks[i]));
    }
    document.getElementsByClassName("step-links-left")[0].addEventListener('click', moveLeft);
    document.getElementsByClassName("step-links-first")[0].addEventListener('click', moveFirst);
    document.getElementsByClassName("step-links-right")[0].addEventListener('click', moveRight);
    document.getElementsByClassName("step-links-last")[0].addEventListener('click', moveLast);
    document.getElementsByClassName("step-links-left")[1].addEventListener('click', moveLeft);
    document.getElementsByClassName("step-links-first")[1].addEventListener('click', moveFirst);
    document.getElementsByClassName("step-links-right")[1].addEventListener('click', moveRight);
    document.getElementsByClassName("step-links-last")[1].addEventListener('click', moveLast);
}

var csrftoken = getCookie('csrftoken');
var navImages = getRelativeImages(current_position, start_position, last_position, 5);
var images = {};
var imageContainer = document.getElementById("img-container");
if (!window.location.origin) {
  window.location.origin = window.location.protocol + "//" + window.location.hostname + (window.location.port ? ':' + window.location.port: '');
}
var base_url = window.location.origin;
var archive_url = window.location.pathname.split("/img/")[0] + "/img/";

images[current_position] = document.getElementById("cent-img");
images[current_position].addEventListener('click', imgClick);
window.addEventListener('touchstart', touchstart);
window.addEventListener('touchend', touchend);
document.getElementById("fullscreen-button").addEventListener('click', contentFullScreen); // MS can only go fullscreen by clicks, not key events.
document.getElementById("save-changes").addEventListener('click', setOptions);

addListenersToNavigators();

addLoadEvent(getNonCachedImagesUrls(archive, navImages[1].concat(navImages[0]).concat(current_position), current_position, false));
    
