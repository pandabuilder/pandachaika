Gallery = function(pk, title, posted){
    this.pk = pk;
    this.title = title;
    this.posted = posted;
    this.x = 0;
    this.y = 0;

};

Diagram1 = function(){

    var gallerySeed;
    var galleryNumber = 0;

    this.boot = function(response){
        gallerySeed = new GallerySeedNew2().boot(response);
        galleryNumber = gallerySeed.allGalleries().length;
        drawBarChart();

    };

    var drawBarChart = function () {

        var margin = {top: 20, right: 20, bottom: 30, left: 40},
            width = 960 - margin.left - margin.right,
            height = 500 - margin.top - margin.bottom;

        var formatDate = d3.time.format("%m/%y");
        // var formatCount = d3.format(",.0f");

        var x = d3.time.scale().range([0, width]);
        var y = d3.scale.linear().range([height, 0]);

        // Determine the first and list dates in the data set
		  var monthExtent = d3.extent(gallerySeed.allGalleries(), function(d) { return d.posted; });

		  // Create one bin per month, use an offset to include the first and last months
		  var monthBins = d3.time.months(d3.time.month.offset(monthExtent[0],-1),
		                                 d3.time.month.offset(monthExtent[1],1));

        var binByMonth = d3.layout.histogram()
		    .value(function(d) { return d.posted; })
		    .bins(monthBins);

        var histData = binByMonth(gallerySeed.allGalleries());

        x.domain(d3.extent(monthBins));
        y.domain([0, d3.max(histData, function(d) { return d.y; })]);

		var xAxis = d3.svg.axis().scale(x)
		  .orient("bottom").tickFormat(formatDate);

		var yAxis = d3.svg.axis().scale(y)
		  .orient("left").ticks(6);


        var svg = d3.select("#diagram-1").append("svg")
        .classed("tag-diagram", true)
            .attr("width", width + margin.left + margin.right)
            .attr("height", height + margin.top + margin.bottom)
        .append("g")
                .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

		  svg.selectAll(".bar")
		      .data(histData)
		    .enter().append("rect")
		      .attr("class", "bar")
		      .attr("x", function(d) { return x(d.x); })
		      .attr("width", function(d) { return x(new Date(d.x.getTime() + d.dx))-x(d.x)-1; })
		      .attr("y", function(d) { return y(d.y); })
		      .attr("height", function(d) { return height - y(d.y); });

		  // Add the X Axis
		  svg.append("g")
		      .attr("class", "x axis")
		      .attr("transform", "translate(0," + height + ")")
		      .call(xAxis);

		  // Add the Y Axis and label
		  svg.append("g")
		     .attr("class", "y axis")
		     .call(yAxis)
		    .append("text")
		      .attr("transform", "rotate(-90)")
		      .attr("y", 6)
		      .attr("dy", ".50em")
		      .style("text-anchor", "end")
		      .text("Number of Releases");

    };

};

function GalleryRetrieve() {
    clearGraph();
    var params = getQuery();
    var xmlhttp = new XMLHttpRequest();
    var lo2 = window.location.href;
    var indices = [];
    for(var i=0; i<lo2.length;i++) {
        if (lo2[i] === "/") indices.push(i);
    }
    var url = lo2.substr(0, indices[indices.length - 2] + 1) + 'posted-seed';
    var response = null;
    var query = [];
    if (params.title)
        query.push("title=" + encodeURIComponent(params.title));
    if (params.tags)
        query.push("tags=" + encodeURIComponent(params.tags));
    url = url + "?" + query.join("&");
    if(query.length === 0) {
        return
    }
    xmlhttp.onreadystatechange = function () {
        if (xmlhttp.readyState === 4 && xmlhttp.status === 200) {
            response = JSON.parse(xmlhttp.responseText);
            if(response.error) {
                $('#search-button').button('reset');
                return
            }
            (new Diagram1).boot(response);
            $('#search-button').button('reset');
            history.replaceState({
            }, document.title, window.location.pathname.split("/gallery-frequency/")[0] + "/gallery-frequency/?" + query.join("&"));
        }
    };
    xmlhttp.open("GET", url, true);
    xmlhttp.send();
    if($('#search-button').button)
        $('#search-button').button('loading');
}

GallerySeedNew2 = function(){
    var galleries = [];
    var tags = [];

    this.boot = function(result){
        if(result.error)
            return null;
        var num_galleries = 0;

        for(var i=0; i < result.length; i++) {
            if(result[i].model === "viewer.gallery"){
                galleries[num_galleries++] = new Gallery(result[i].pk, result[i].fields.title, d3.time.format.iso.parse(result[i].fields.posted));
            }
        }
        return this;
    };

    this.allGalleries = function(){return galleries};
    this.allTags = function(){return tags};
};

function getQuery() {
    var query = {
        title: document.getElementById("id_title").value,
        tags: document.getElementById("id_tags").value
    };
    return (query);
}

function clearGraph() {
    var x = document.getElementById("diagram-1");
    x.innerHTML = "";
}

document.getElementById("send-data").addEventListener("submit", function(e){
    GalleryRetrieve();
    e.preventDefault();
});

document.getElementById("send-data").addEventListener("reset", function(e){
    history.replaceState({
    }, document.title, window.location.pathname.split("/gallery-frequency/")[0] + "/gallery-frequency/");
    document.getElementById("id_title").value = '';
    document.getElementById("id_tags").value = '';
    clearGraph();
    e.preventDefault();
});

function addLoadEvent(func) {
	var old_onload = window.onload;
	if (typeof window.onload !== 'function') {
		window.onload = func;
	} else {
		window.onload = function() {
			if (old_onload) {
				old_onload();
			}
			func();
		}
	}
}

// Loading button plugin (removed from BS4)
(function($) {
  $.fn.button = function(action) {
    if (action === 'loading' && this.data('loading-text')) {
      this.data('original-text', this.html()).html(this.data('loading-text')).prop('disabled', true);
    }
    if (action === 'reset' && this.data('original-text')) {
      this.html(this.data('original-text')).prop('disabled', false);
    }
  };
}(jQuery));

addLoadEvent(GalleryRetrieve());