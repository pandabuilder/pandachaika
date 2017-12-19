(function() {
    window.Bindings = Keys.Bindings;
    window.Combo    = Keys.Combo;
    window.Key      = Keys.Key;

    // Initialize application-wide bindings manager
    window.bindings = new Bindings();

    bindings.load({
        toggleArchive: {
            bind: new Combo(Key.E, Key.SHIFT),
            handler: toggleArchive
        },
        fullScreen: {
            bind: new Combo(Key.F, Key.SHIFT),
            handler: contentFullScreen
        },
        moveLeft: {
            bind: new Combo(Key.Left),
            handler: goPreviousImage
        },
        moveRight: {
            bind: new Combo(Key.Right),
            handler: goNextImage
        },
        previousPage: {
            bind: new Combo(Key.Left, Key.SHIFT),
            handler: previousPage
        },
        nextPage: {
            bind: new Combo(Key.Right, Key.SHIFT),
            handler: nextPage
        }
        //hideModal: {
        //   bind: new Combo(Key.X, Key.SHIFT),
        //    eventType: 'keypress',
        //    handler: hideModal
        //}
    });

    // Add binding to display an alert
    // bindings.add('moveRight', new Combo(Key.A));
    // bindings.add('moveLeft', new Combo(Key.LEFT));

    // Register display alert behavior using inferred name/eventType notation
    // bindings.registerHandler(displayAlert);
    /**
     *  If you want to use a specific eventType (default is 'keydown'):
    bindings.registerHandler('keyup', displayAlert);

     *  Or if you want to use a named function for a binding with a different name:
    bindings.registerHandler('sayHello', displayAlert);

     *  Or if you want to use an anonymous function for the handler:
    bindings.registerHandler('displayAlert', function() {
        alert('Hello!');
    });

     *  Or if you want to specify everything at once:
    bindings.registerHandler('displayAlert', 'keyup', displayAlert);

     */
    function toggleArchive() {
    	window.location.replace("extract-toggle/");
    }
    function contentFullScreen() {
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
    function goNextImage() {
        if(window.location.hash && window.location.hash.startsWith("#img-")) {
          // Fragment exists
            var num = parseInt(window.location.hash.substr(5));
            if(num < numImages){
                window.location.hash = "#img-" + (num + 1);
            }
        } else {
          // Fragment doesn't exist
            window.location.hash = "#img-1";
        }
    }
    function goPreviousImage() {
        if(window.location.hash && window.location.hash.startsWith("#img-")) {
          // Fragment exists
            var num = parseInt(window.location.hash.substr(5));
            if(num > 1){
                window.location.hash = "#img-" + (num - 1);
            }
        } else {
          // Fragment doesn't exist
            window.location.hash = "#img-1";
        }
    }
    function previousPage() {
        var nav_1 = document.getElementById("prev-page");
        if(nav_1)
            window.location.replace(nav_1.href + "#img-1");
    }
    function nextPage() {
	    var nav_1 = document.getElementById("next-page");
	    if(nav_1)
	        window.location.replace(nav_1.href + "#img-1");
    }
})();
    
