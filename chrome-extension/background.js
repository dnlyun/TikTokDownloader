"use strict";

chrome.action.onClicked.addListener(async tab => {
  if (!tab.id || !/^https:\/\/(?:www\.)?tiktok\.com\//i.test(tab.url || "")) {
    return;
  }

  try {
    await chrome.scripting.insertCSS({
      target: { tabId: tab.id },
      files: ["selector.css"],
    });
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["selection-model.js", "selector.js"],
    });
  } catch (error) {
    console.error("Could not start TikTok link selector", error);
  }
});
