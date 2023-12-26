document.querySelector("#toggle-all")
    .addEventListener("click", () => {
        document.querySelectorAll("input.toggle-list").forEach((element) => element.click());
});

document.addEventListener("DOMContentLoaded", function(){
    const checkboxes = Array.from(document.querySelectorAll("input.toggle-list"));
    let lastChecked = null;

    checkboxes.forEach((checkbox) => {
        checkbox.addEventListener('click', function(e) {
            if(!lastChecked) {
                lastChecked = checkbox;
                return;
            }

            if(e.shiftKey) {
                const start = checkboxes.indexOf(checkbox);
                const end = checkboxes.indexOf(lastChecked);

                checkboxes.slice(Math.min(start,end), Math.max(start,end)+ 1).forEach((slicedCheckbox) => {
                    slicedCheckbox.checked = lastChecked.checked;
                });

            }

            lastChecked = checkbox;
        });
        checkbox.addEventListener('mousedown', function(e) {
            if(e.shiftKey) {
                e.preventDefault();
            }
        });
    })

});