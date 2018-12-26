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
        return '<img src="'+$(this).data('imageUrl')+'">';
    }
});