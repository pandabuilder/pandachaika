$('#more').click(function () {
if($('button span').hasClass('glyphicon-chevron-down'))
{
   $('#more').html('<span class="glyphicon glyphicon-chevron-up"></span>');
}
else
{      
    $('#more').html('<span class="glyphicon glyphicon-chevron-down"></span>');
}
});

$('.img-preview').popover({
    trigger : 'hover',
    html : true,
    content : function(){
        return '<img src="'+$(this).data('imageUrl')+'">';
    }
});