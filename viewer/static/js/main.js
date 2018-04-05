$('#more').click(function () {
if($('button span').hasClass('fa-chevron-down'))
{
   $('#more').html('<span class="fas fa-chevron-up"></span>');
}
else
{      
    $('#more').html('<span class="fas fa-chevron-down"></span>');
}
});

$('.img-preview').popover({
    trigger : 'hover',
    html : true,
    content : function(){
        return '<img src="'+$(this).data('imageUrl')+'">';
    }
});