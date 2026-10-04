# ScreenWatcher

## Introduction

![icon](./Resources/icons/icon.ico)

ScreenWatcher is a desktop application that freeing you from staring at the screen. You can use it when you have to constantly pay attention to certain images that may appear on the screen. When the image that matches the specified template appears in a specific area of the screen, ScreenWatcher will make a sound to alert you.

ScreenWatcher is a python project based on Pyside6 and Opencv. Pyside6 provides the window user interface. The template matching API of OpenCV is utilized for the purpose of object detection that aligns with your visual focus.

## Installation and Usage Instructions

You can start ScreenWatcher through the source code or the exe file in Releases.

### Through Source Code

First, clone the code.

```cmd
git clone git@github.com:bigzeze/ScreenWatcher.git
```

Then, prepare the environment. ScreenWatcher is developed under Python 3.10.0.

You can automatically install the requirements by runing the  command below or install manually according to the requirements.txt.

```cmd
pip install -r requirements.txt
```

At last, run it.

```cmd
python src/ScreenWatcher.py
```

### Through Exe File

Make sure that '_internal' and 'icons'  directories and their contents exists.

Then double click the file 'ScreenWatcher.exe' to start, all the parameters can be configed through the UI.

### Usage

To add multiple watchers, click 'Setting' to name them and set up the templates, alert audio,and detect interval separately.

Click on the 'Select Area' button, hold down the mouse to slide over an area where you would like the watcher to stare at.

Push the 'Start Watch' button to start each screen watcher, and push the 'End Watch' to stop.

Right click  the tray icon and click 'quit' to close the software.

The detect log is saved in 'log.txt'.

## Update Log

v1.0  first release

v1.1  add settings of template detection threshold

v1.2  add support for macos, dockwidget can be stacked when loaded.

# 屏幕监视器

## 介绍

![icon](./Resources/icons/icon.ico)

**屏幕监视器**是一个桌面程序，用于代替您盯着屏幕。当指定的图像出现在屏幕的特定区域时，屏幕监控器会发出声音提醒您。

这是一个由Pyside6和Opencv制作的python项目。Pyside6提供了窗口用户界面。OpenCV的模板匹配 API用来检测屏幕上是否出现指定图像。

## 安装和使用

您可以通过源代码或Releases中的exe文件启动**屏幕监视器**。

### 通过源代码

首先下载代码。

```cmd
git clone git@github.com:bigzeze/ScreenWatcher.git
```

然后准备运行环境。**屏幕监视器**是在Python 3.10.0环境下开发的。

你可以通过运行以下命令自动安装环境，也可以根据requirements.txt手动配置环境。

```cmd
pip install -r requirements.txt
```

最后，运行！

```cmd
python src/ScreenWatcher.py
```

### 通过exe文件

确保_internal、icons文件夹里的内容没有丢失。

双击*ScreenWatcher.exe*运行。

### 使用

添加多个监控器，点击Setting按钮，设置每个监控器的名称、模版、提醒声音和检测时间间隔。

点击Select Area按钮，按住鼠标拖出你想要监控的屏幕区域。

点击Start Watch按钮，监控器开始工作，点击End Watch按钮，监控器停止工作。

右键点击任务栏图标，按quit关闭程序。

检测日志保存在log.txt文件。

## 更新日志

v1.0  发布

v1.1  可以设置模版检测的阈值

v1.2  增加对mac系统的支持，窗口加载时可以堆叠起来。
