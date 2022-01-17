$('#more').click(function () {
if($('#more i').hasClass('fa-chevron-down'))
{
   $('#more').html('<i class="fas fa-chevron-up"></i>');
}
else
{
    $('#more').html('<i class="fas fa-chevron-down"></i>');
}
});

$('.img-preview').popover({
    trigger : 'hover',
    html : true,
    content : function(){
        return $(this).data('height') ?
        '<img height="' + $(this).data('height') + '" width="' + $(this).data('width') + '" src="'+$(this).data('imageUrl')+'">' :
            '<img src="'+$(this).data('imageUrl')+'">';
    }
});