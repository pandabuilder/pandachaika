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
        openViewer: {
            bind: new Combo(Key.V, Key.SHIFT),
            handler: openViewer
        },
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
    function openViewer() {
	    const button = document.getElementById("view-online");
	    if(button)
	        window.location.assign(button.href);
    }
})();
