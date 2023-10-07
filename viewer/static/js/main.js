document.querySelectorAll(".collapse-icon").forEach(element => {
    element.addEventListener("click", (event) => {
        const imgElement = event.currentTarget.querySelector('i');
        if (imgElement.classList.contains('fa-chevron-down')) {
            imgElement.classList = "fas fa-chevron-up";
        } else {
            imgElement.classList = "fas fa-chevron-down";
        }
    });
});


const popoverTriggerList = document.querySelectorAll('.img-preview')
const popoverList = [...popoverTriggerList].map(popoverTriggerEl => new bootstrap.Popover(popoverTriggerEl, {
    trigger : 'hover',
    html : true,
    content : function(){
        return popoverTriggerEl.dataset['height'] ?
        '<img alt="" height="' + popoverTriggerEl.dataset['height'] + '" width="' + popoverTriggerEl.dataset['width'] + '" src="'+popoverTriggerEl.dataset['imageUrl']+'">' :
            '<img alt="" src="'+popoverTriggerEl.dataset['imageUrl']+'">';
    }
}))