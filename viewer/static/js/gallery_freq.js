Gallery = function(pk, title, posted){
    this.pk = pk;
    this.title = title;
    this.posted = posted;
    this.x = 0;
    this.y = 0;

};

Diagram1 = function(){

    this.gallerySeed = null;

    this.boot = function(response){
        this.gallerySeed = new GallerySeedNew2().boot(response);
        this.drawBarChart(this.gallerySeed);
    };

    this.drawBarChart = function (gallerySeed) {

        var margin = {top: 20, right: 20, bottom: 30, left: 50},
            width = 1000 - margin.left - margin.right,
            height = 500 - margin.top - margin.bottom;

        var formatDate = d3.time.format("%m/%y");
        // var formatCount = d3.format(",.0f");

        var x = d3.time.scale().range([0, width]);
        var y = d3.scale.linear().range([height, 0]);

        // Determine the first and list dates in the data set
		  var monthExtent = d3.extent(gallerySeed.allGalleries(), function(d) { return d.posted; });

		  var binPeriod = getBinPeriod();

		  var Bins;

          if (binPeriod === 'month') {
		  // Create one bin per month, use an offset to include the first and last months
              Bins = d3.time.months(d3.time.month.offset(monthExtent[0],-1),
                                             d3.time.month.offset(monthExtent[1],1));
          }
          else {
              Bins = d3.time.years(d3.time.year.offset(monthExtent[0],-1),
                                             d3.time.year.offset(monthExtent[1],1));
          }


        var binByChosenPeriod = d3.layout.histogram()
		    .value(function(d) { return d.posted; })
		    .bins(Bins);

        var histData = binByChosenPeriod(gallerySeed.allGalleries());

        x.domain(d3.extent(Bins));
        y.domain([0, d3.max(histData, function(d) { return d.y; })]);

		var xAxis = d3.svg.axis().scale(x)
		  .orient("bottom").tickFormat(formatDate);

		var yAxis = d3.svg.axis().scale(y).tickFormat(d3.format("s"))
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

function getBinPeriod() {
    if(document.getElementById("bin-month").checked) {
        return 'month';
    } else if (document.getElementById("bin-year").checked) {
        return 'year';
    }
    return 'month';
}

var currentDiagram = null;

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
    var queryString = new URLSearchParams(params).toString();
    url = url + "?" + queryString;
    xmlhttp.onreadystatechange = function () {
        if (xmlhttp.readyState === 4 && xmlhttp.status === 200) {
            response = JSON.parse(xmlhttp.responseText);
            if(response.error) {
                $('#search-button').button('reset');
                return
            }
            currentDiagram = new Diagram1();
            currentDiagram.boot(response);
            $('#search-button').button('reset');
            history.replaceState({
            }, document.title, window.location.pathname.split("/gallery-frequency/")[0] + "/gallery-frequency/?" + queryString);
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
    };
    
    var title = document.getElementById("id_title").value;
    var tags = document.getElementById("id_tags").value;
    var filecount_from = document.getElementById("id_filecount_from").value;
    var filecount_to = document.getElementById("id_filecount_to").value;
    var posted_from = document.getElementById("id_posted_from").value;
    var posted_to = document.getElementById("id_posted_to").value;
    var provider = document.getElementById("id_provider").value;
    var reason = document.getElementById("id_reason").value;
    var uploader = document.getElementById("id_uploader").value;
    var category = document.getElementById("id_category").value;
    var filesize_from = document.getElementById("id_filesize_from").value;
    var filesize_to = document.getElementById("id_filesize_to").value;

    if(title) {
        query['title'] = title;
    }
    if(tags) {
        query['tags'] = tags;
    }
    if(filecount_from) {
        query['filecount_from'] = filecount_from;
    }
    if(filecount_to) {
        query['filecount_to'] = filecount_to;
    }
    if(posted_from) {
        query['posted_from'] = posted_from;
    }
    if(posted_to) {
        query['posted_to'] = posted_to;
    }
    if(provider) {
        query['provider'] = provider;
    }
    if(reason) {
        query['reason'] = reason;
    }
    if(uploader) {
        query['uploader'] = uploader;
    }
    if(category) {
        query['category'] = category;
    }
    if(filesize_from) {
        query['filesize_from'] = filesize_from;
    }
    if(filesize_to) {
        query['filesize_to'] = filesize_to;
    }
    
    return (query);
}

function clearGraph() {
    var x = document.getElementById("diagram-1");
    x.innerHTML = "";
    return true;
}

document.getElementById("bin-month").addEventListener("change", function(e){
    currentDiagram != null ? clearGraph() && currentDiagram.drawBarChart(currentDiagram.gallerySeed) : null;
});

document.getElementById("bin-year").addEventListener("change", function(e){
    currentDiagram != null ? clearGraph() && currentDiagram.drawBarChart(currentDiagram.gallerySeed) : null;
});

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