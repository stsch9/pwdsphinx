/*
 * This file is part of WebSphinx.
 * Copyright (C) 2018 pitchfork@ctrlc.hu
 *
 * WebSphinx is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * WebSphinx is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License along
 * with this program; if not, write to the Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
 */

"use strict";

const APP_NAME = "websphinx";

var browser = browser || chrome;
var ports = {};
var nativeport = browser.runtime.connectNative(APP_NAME);
var changeData = true;

nativeport.onMessage.addListener((response) => {
  // internal error handling
  if (browser.runtime.lastError) {
    var error = browser.runtime.lastError.message;
    console.error(error);
    ports['popup'].postMessage({ status: "ERROR", error: error });
    return;
  }

  // client error handling
  if(response.results == 'fail') {
    if(response.cmd) {
        response.results = {'error': true, 'id': response.id, 'cmd': response.cmd};
        const tabId = Number(response.tabId);
        browser.tabs.sendMessage(tabId, response);
    }
    console.log('websphinx failed');
    return;
  }

  // handle manual inserts
  if(response.results.mode == "manual") {
    //console.log("manual");
    // its a manual mode response so we just pass it to the popup
    ports['popup'].postMessage(response);
    return;
  }

  // handle get current pwd
  if(response.results.cmd == 'login') {
    // 1st step in an automatic change pwd
    if(changeData==true) {
      // we got the old password
      changeData={password: response.results.password};
      // now change the password
      response.results.cmd="change";
      delete response.results['password']; // don't send the password back
      nativeport.postMessage(response.results);
      return;
    }
    let login = {
      username: response.results.name,
      password: response.results.password
    };
    browser.tabs.executeScript({code: 'document.websphinx.login(' + JSON.stringify(login) + ');'});
    return;
  }

  // handle webauthn create
  if(response.results.cmd == 'webauthn-create') {
    const tabId = Number(response.results.tabId);
    browser.tabs.sendMessage(tabId, response);
    return;
  }

  // handle webauthn get
  if(response.results.cmd == 'webauthn-get') {
    const tabId = Number(response.results.tabId);
    browser.tabs.sendMessage(tabId, response);
    return;
  }

  // handle list users
  if(response.results.cmd == 'list') {
    ports['popup'].postMessage(response);
    return;
  }

  // handle create password
  if(response.results.cmd == 'create') {
    let account = {
      username: response.results.name,
      password: response.results.password
    };
    browser.tabs.executeScript({code: 'document.websphinx.create(' + JSON.stringify(account) + ');'});
    return;
  }

  // handle change password
  if(response.results.cmd == 'change') {
    let change = {
      'old': changeData,
      'new': response.results
    }
    browser.tabs.executeScript({code: 'document.websphinx.change(' + JSON.stringify(change) + ');'});
    changeData = false;
    return;
  }

  // handle commit result
  if(response.results.cmd == 'commit') {
    ports['popup'].postMessage(response);
    return;
  }
  console.log("unhandled native port response");
  console.log(response);
});

browser.runtime.onConnect.addListener(function(p) {
  ports[p.name] = p;

  if(p.name == 'popup') {
	  // proxy from CS to native backend
	  p.onMessage.addListener(function(request, sender, sendResponse) {
		// prepare message to native backend
		let msg = {
			cmd: request.action,
			mode: request.mode,
			site: request.site
		};

		if(request.action!="list") msg.name=request.name;
		if (request.action == "login") changeData=false;
		if (request.action == "create") {
		  msg.rules= request.rules;
		  msg.size= request.size;
		}
		if (request.action == "change") {
		  if(request.mode != "manual") {
			// first get old password
			// but this will trigger the login inject in the nativport onmessage cb
			changeData = true;
			msg.cmd= "login";
		  }
		}

		if(request.action!="login" &&
		   request.action!="list" &&
		   request.action!="create" &&
		   request.action!="change" &&
		   request.action!="commit") {
		  console.log("unhandled popup request");
		  console.log(request);
		  return;
		}

		// send request to native backend
		nativeport.postMessage(msg);
	  });
  }
  if(p.name == 'content-script') {
	  p.onMessage.addListener(function(request, sender, sendResponse) {
		let msg = {
			cmd: request.action,
			mode: request.mode,
			site: request.site,
			challenge: request.params.challenge,
			name: request.params.username,
			id: request.id,
		};
		nativeport.postMessage(msg);
	  });
  }
});

// handle "synchronous" calls for webauthn
chrome.runtime.onMessage.addListener(
	function(request, sender, sendResponse) {
        if(!request.action || !request.action.startsWith('webauthn')) {
            return;
        }
        let msg = {}
        const tabId = sender.tab.id;
        if(request.action == "webauthn-create") {
            msg = {
                cmd: request.action,
                mode: request.mode,
                site: request.site,
                clientDataJSON: request.params.clientDataJSON,
                challenge: request.params.challenge,
                name: request.params.username,
                userid: request.params.userid,
                id: request.id,
                tabId: tabId,
            };
        }
        if(request.action == "webauthn-get") {
            msg = {
                cmd: request.action,
                mode: request.mode,
                site: request.site,
                clientDataJSON: request.params.clientDataJSON,
                challenge: request.params.challenge,
                pk: request.params.pk,
                name: request.params.username,
                userid: request.params.userid,
                id: request.id,
                tabId: tabId,
            };
        }
		nativeport.postMessage(msg);
	}
);
