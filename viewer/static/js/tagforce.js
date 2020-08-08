Post = function(pk, name){
    this.pk = pk;
    this.name = name;
    this.tags = {};
    this.tagKeys = [];
    this.x = 0;
    this.y = 0;

    this.addTag = function(tag){
        this.tagKeys.push(tag.pk);
        this.tags[tag.pk] = tag;
        tag.addPost(this);
    };
};

Tag = function(pk){
    this.pk = pk;
    this.name = '';
    this.posts = {};
    this.postKeys = [];
    this.frequency = 0;

    this.addPost = function(post){
        this.postKeys.push(post.pk);
        this.posts[post.pk] = post;
    };

    this.hasPost = function(post){
        return this.posts[post.pk] !== null;
    };
};

Diagram1 = function(){

    var postSeed;
    var postNumber = 0;
    var tagNumber = 0;

    this.boot = function(response){
        postSeed = new GallerySeedNew2().boot(response);
        postNumber = postSeed.allPosts().length;
        tagNumber = postSeed.allTags().length;
        drawBarChart();

    };

    var drawBarChart = function () {

        var nodes = postSeed.allTags(),
            n = nodes.length;

        var margin = {top: 20, right: 20, bottom: 30, left: 40},
            width = 960 - margin.left - margin.right,
            height = n * 21 - margin.top - margin.bottom;

        var x = d3.scale.linear()
            .range([0, width]);

        var y = d3.scale.ordinal()
            .rangeBands([0, height], .1, 2);

        var xAxis = d3.svg.axis()
            .scale(x)
            .orient("top")
            .ticks(10, "%");

        var yAxis = d3.svg.axis()
            .scale(y)
            .orient("right");


        var svg = d3.select("#diagram-1").append("svg")
        .classed("tag-diagram", true)
            .attr("width", width + margin.left + margin.right)
            .attr("height", height + margin.top + margin.bottom);


          nodes.forEach(function(node, i) {
            node.index = i;
            node.count = 0;
            node.frequency = Object.keys(nodes[i].posts).length / postNumber;
            node.occurence = Object.keys(nodes[i].posts).length;
            //data[i] = d3.range(n).map(function(j) { return {x: j, y: i, z: 0}; });
          });

      // Precompute the orders.
        var orders = {
        name: d3.range(n).sort(function(a, b) { return d3.ascending(nodes[a].name, nodes[b].name); }).map(function(d) { return nodes[d].name; }),
        frequency: d3.range(n).sort(function(a, b) { return nodes[b].frequency - nodes[a].frequency; }).map(function(d) { return nodes[d].name; })
        //group: d3.range(n).sort(function(a, b) { return nodes[b].group - nodes[a].group; })
        };

        // The default sort order.
        //y.domain(orders.name);

        x.domain([0, d3.max(nodes, function(d) { return d.frequency; })]);
        //y.domain(nodes.map(function(d) { return d.name; }));
        y.domain(orders.frequency);

        //Tooltip
        var tip = d3.tip()
          .attr('class', 'd3-tip')
          .direction('e')
          .offset(function(d) {
            return [(d.occurence * 14 + 12 + 12)/2, 10]
          })
          .html(function(d) {
            return Object.keys(d.posts).map(function(postId) {
            return "<div><a href='/gallery/" + postId + "/'>" + d.posts[postId].name + "</a></div>";
            //data[i] = d3.range(n).map(function(j) { return {x: j, y: i, z: 0}; });
          }).join('');
          });

        svg.call(tip);

        var chart = svg.append("g")
                .attr("transform", "translate(" + margin.left + "," + margin.top + ")");

        chart.append("g")
          .attr("class", "x axis")
          .call(xAxis)
        .append("text")
          //.attr("transform", "rotate(-90)")
          .attr("x", 55)
            .attr("y", 25)
          .attr("dx", ".71em")
          .style("text-anchor", "end")
          .text("Frequency");

        chart.selectAll(".bar")
          .data(nodes)
        .enter().append("rect")
          .attr("class", "bar")
          .attr("x", x(0))
          .attr("width", function(d) { return x(d.frequency); })
          .attr("y", function(d) { return y(d.name); })
          .attr("height", y.rangeBand())
          .on('click', function(d) {
            tip.hide(d).show(d);
          });
          // .on('mouseover', tip.show)
          // .on('mouseout', tip.hide)

        chart.selectAll(".text")
          .data(nodes)
        .enter().append("svg:text")
         .attr("x", x(0))
         .attr("y", function(d) { return y(d.name); })
         .attr("dx", -3) // padding-right
         .attr("dy", "1.32em") // vertical-align: middle
         .attr("text-anchor", "end") // text-align: right
         .text(function(d) { return d.occurence; });

        chart.append("g")
          .attr("class", "y axis")
          .call(yAxis);

    };

};

function GalleryTagRetrieve() {
    clearGraph();
    var params = getQuery();
    var xmlhttp = new XMLHttpRequest();
    var lo2 = window.location.href;
    var indices = [];
    for(var i=0; i<lo2.length;i++) {
        if (lo2[i] === "/") indices.push(i);
    }
    var url = lo2.substr(0, indices[indices.length - 2] + 1) + 'seed';
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
            }, document.title, window.location.pathname.split("/tag-frequency/")[0] + "/tag-frequency/?" + query.join("&"));
        }
    };
    xmlhttp.open("GET", url, true);
    xmlhttp.send();
    if($('#search-button').button)
        $('#search-button').button('loading');
}

GallerySeedNew2 = function(){
    var posts = [];
    var tags = [];
    var tag_hash = {};

    this.boot = function(result){
        if(result.error)
            return null;
        var num_posts = 0;
        var num_tags = 0;

        for(var i=0; i < result.length; i++) {
            if(result[i].model === "viewer.gallery"){
                var new_post = new Post(result[i].pk, result[i].fields.title);
                posts[num_posts++] = new_post;
                for(var j=0; j < result[i].fields.tags.length; j++){
                    var tag_pk = result[i].fields.tags[j];
                    new_post.addTag(tag_hash[tag_pk]);

                }
            }
            else if(result[i].model === "viewer.tag"){
                var new_tag = new Tag(result[i].pk);
                if(result[i].fields.scope !== '')
                    new_tag.name = result[i].fields.scope + ":" + result[i].fields.name;
                else
                    new_tag.name = result[i].fields.name;
                tags[num_tags++] = new_tag;
                tag_hash[result[i].pk] = new_tag;
            }

        }
        return this;
    };

    this.allPosts = function(){return posts};
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
    GalleryTagRetrieve();
    e.preventDefault();
});

document.getElementById("send-data").addEventListener("reset", function(e){
    history.replaceState({
    }, document.title, window.location.pathname.split("/tag-frequency/")[0] + "/tag-frequency/");
    document.getElementById("id_title").value = '';
    document.getElementById("id_tags").value = '';
    clearGraph();
    e.preventDefault();
});

function addLoadEvent(func) {
	var oldonload = window.onload;
	if (typeof window.onload !== 'function') {
		window.onload = func;
	} else {
		window.onload = function() {
			if (oldonload) {
				oldonload();
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

addLoadEvent(GalleryTagRetrieve());