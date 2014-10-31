from __future__ import print_function, division
from datetime import datetime
import time

from replay.pipeline.pipeline import (DataMuggler, PipelineComponent,
                                      MuggleWatcherLatest, DmImgSequence)


from replay.model.scalar_model import ScalarCollection
from replay.model.cross_section_model import CrossSectionModel
from enaml.qt.qt_application import QtApplication
import enaml

import matplotlib.pyplot as plt
from nsls2 import core
from enaml.qt import QtCore
import numpy as np
# from bubblegum.backend.mpl.cross_section_2d import (absolute_limit_factory,
#                                                     CrossSection)
from nsls2.fitting.model.physics_model import GaussianModel
import lmfit

import socket
import select
import broker.config as cfg
import json


def plotter(title, xlabel, ylabel, ax=None, N=None, ln_sty=None, fit=False):
    """
    This function generates a function which will
    take two lists and plot them against each other.

    If an axes is not passed in, create a new figure + axes
    else, use the one that is passed in.

    .. Warning : If ax axes is passed in, the labels are ignored.  This
        is bad API design.  What idiot wrote this?

    Parameters
    ----------
    title : str
        Axes title

    xlabel : str
        X-axis label

    ylabel : str
        Y-axis label

    ax : Axes, optional
        if not given or None, create new figure, else draw to the one
        passed in.

    N : int, optional
        Only plot the last N points

    ln_sty : dict, optional
        dictionary of kwargs to be unpacked into the plot call

        CURRENTLY IGNORED

    fit : bool, optional
        If should try to fit

    Returns
    -------
    callabale
        A callable with the signature ::

            def inner(x, y):
                '''
                Parameters
                ----------
                x : list
                    x-data
                y : list
                    y-data

                '''
                return None
    """
    if ax is None:
        fig, ax = plt.subplots(1, 1)

    if ln_sty is None:
        ln_sty = dict()

        ax.set_title(title)
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        txt = ax.annotate('', (0, 0), xytext=(1, 1), xycoords='axes fraction')

    ln_sty = 'bo-'
    if fit:
        ln_sty = 'bo'

    ln, = ax.plot([], [], ln_sty)
    if fit:
        ln2, = ax.plot([], [], 'g-')
        m = GaussianModel() + lmfit.models.ConstantModel()
        param = m.make_params()
        for k in param:
            param[k].value = 1

        param['area'].min = 1
        param['area'].max = 150
        param['sigma'].min = 1
        param['sigma'].max = 150
        param['center'].min = 0
        param['center'].max = 150
    time_tracker = {'old': time.time()}

    def inner(y, x):
        '''
        Update line with this data.  relim, autoscale, trigger redraw

        Parameters
        ----------
        x : list
            x-data
        y : list
            y-data

        '''
        if N is not None:
            x = x[:N]
            y = y[:N]

        ln.set_data(x, y)
        if fit and len(x) > 4:
            param['c'].value = np.min(y)
            param['center'].value = x[np.argmax(y)]
            res = m.fit(y, x=x, params=param)
            # try to be clever and iterative
            param.update(res.params)
            ft_y = res.eval()
            ln2.set_data(x, ft_y)

        # this should include blitting logic
        ax.relim()
        ax.autoscale_view(False, True, True)
        cur = time.time()
        txt.set_text(str(cur - time_tracker['old']))
        time_tracker['old'] = cur
        ax.figure.canvas.draw()
        #        plt.pause(.1)

    return inner

# def imshower():
#     fig = plt.figure()
#     xsection = CrossSection(fig, interpolation='none',
#                             limit_func=absolute_limit_factory((0, 1.5))
#     )
#
#     def inner(msg, data):
#         xsection.update_image(data['img'])
#
#     return inner


# stolen from other live demo
class FrameSourcerBrownian(QtCore.QObject):
    """
    A QObject that has a timer and will emit synthetic data
    of a dot moving around under brownian motion with varying intensity

    Parameters
    ----------
    im_shape : tuple
        The shape of the image.  The synthetic images gets emitted with the
        label 'img'

    step_scale : float, optional
        The size of the random steps.  This value get emitted with the label
        'T'

    decay : float, optional
        The size of the spot

    delay : int, optional
        The timer delay in ms

    parent : QObject, optional
        Qt parent

    max_count : int, optional
        After this many images stop self.  Default to MAXINT64

    I_fluc_function : callable
        Determine the maximum intensity of the spot as a function of count

        Signature of ::

            def func(count):
                return I(count)

    step_fluc_function : callable
         Determine if step should change and new step value.  Either return
         the new step value or None.  If the new step is None, then don't emit
         a 'T' event, other wise change the temperature and emit the event

         Signature of ::

             def func(step, count):
                 if not change_step(count):
                     return new_step(step, count)
                 else:
                     return None
    """
    event = QtCore.Signal(object, dict)

    def __init__(self, im_shape, step_scale=1, decay=30,
                 delay=500, parent=None, max_count=None,
                 I_fluc_function=None, step_fluc_function=None):
        QtCore.QObject.__init__(self, parent)
        self._im_shape = np.asarray(im_shape)
        self._scale = step_scale
        self._decay = decay
        self._delay = delay
        if max_count is None:
            max_count = np.iinfo(np.int64).max
        self._max_count = max_count

        if I_fluc_function is None:
            I_fluc_function = lambda x: 1

        self._I_func = I_fluc_function

        if step_fluc_function is None:
            step_fluc_function = lambda step, count: None

        self._scale_func = step_fluc_function

        if self._im_shape.ndim != 1 and len(self._im_shape) != 2:
            raise ValueError("image shape must be 2 dimensional "
                             "you passed in {}".format(im_shape))
        self._cur_position = np.array(np.asarray(im_shape) / 2, dtype=np.float)

        self.timer = QtCore.QTimer(parent=self)
        self.timer.timeout.connect(self.get_next_frame)
        self._count = 0

    @QtCore.Slot()
    def get_next_frame(self):
        self._count += 1

        new_scale = self._scale_func(self._scale, self._count)
        if new_scale is not None:
            self._scale = new_scale
            self.event.emit(datetime.now(), {'T': self._scale})

        im = self.gen_next_frame()
        self.event.emit(datetime.now(), {'img': im, 'count': self._count})

        if self._count > self._max_count:
            self.stop()
        print('fired {}, scale: {}, cur_pos: {}'.format(self._count,
                                                        self._scale,
                                                        self._cur_position))
        return True

    def gen_next_frame(self):
        # add a random step
        step = np.random.randn(2) * self._scale
        self._cur_position += step
        # clip it
        self._cur_position = np.array([np.clip(v, 0, mx) for
                                       v, mx in zip(self._cur_position,
                                                    self._im_shape)])
        R = core.pixel_to_radius(self._im_shape,
                                 self._cur_position).reshape(self._im_shape)
        I = self._I_func(self._count)
        im = np.exp((-R**2 / self._decay)) * I
        return im

    @QtCore.Slot()
    def start(self):
        self._count = 0
        # make sure we have a starting temperature event
        self.event.emit(datetime.now(), {'T': self._scale})
        self.timer.start(self._delay)

    @QtCore.Slot()
    def stop(self):
        self.timer.stop()


class ArmanWorker(QtCore.QObject):
    event = QtCore.Signal(object, dict)
    read = QtCore.Signal()

    def __init__(self, parent=None):
        QtCore.QObject.__init__(self, parent)

        self.read.connect(self.self_spammer)

    def self_spammer(self):
        print (QtCore.QThread.currentThreadId())
        print("self spam spam spam spam")

    def read_socket(self):
        print('reading')
        print (QtCore.QThread.currentThreadId())
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((cfg.SEND_HOST, cfg.SEND_PORT))
        print('sockets connect')
        ready = select.select([s], [], [], 10)
        if ready[0]:
            print('ready')
            accum_data = []
            data = s.recv(4096)
            print('read once')
            while len(data):
                accum_data.append(data)
                data = s.recv(4096)
            data = ''.join(accum_data)
            print('joined')
            if data:
                my_data = json.loads(data)
                print('loaded')
                for d in my_data:
                    if 'img' in d:
                        d['img'] = np.asarray(d['img'])
                    print('emitted ', list(d))
                    self.event.emit(datetime.now(), d)

        s.close()
        self.read.emit()


class ArmanListener(QtCore.QObject):
    """
    Class to listen to the first draft of the socket-based I/O
    """
    event = QtCore.Signal(object, dict)
    trigger_read = QtCore.Signal()
    def __init__(self, parent=None, **kwargs):
        QtCore.QObject.__init__(self, parent=parent, **kwargs)
        self.worker = ArmanWorker()

        self.thread = QtCore.QThread(parent=self)
        self.worker.moveToThread(self.thread)

        self.worker.event.connect(self.event.emit)
        self.worker.read.connect(self.spammer)
        self.worker.read.connect(self.feedback)
        self.trigger_read.connect(self.spammer)
        self.trigger_read.connect(self.worker.read_socket)
        self.trigger_read.connect(self.worker.self_spammer)

        self.thread.start()
    def spammer(self):
        print (QtCore.QThread.currentThreadId())
        print("SPAM")

    def start(self):
        self.trigger_read.emit()

    def feedback(self):
        self.trigger_read.emit()


# used below
img_size = (150, 150)
period = 150
I_func_sin = lambda count: (1 + .5*np.sin(2 * count * np.pi / period))
center = 2000
sigma = 1250
I_func_gaus = lambda count: (1 + np.exp(-((count - center)/sigma) ** 2))


def scale_fluc(scale, count):
    if not count % 50:
        return scale - .5
    if not count % 25:
        return scale + .5
    return None

# frame_source = FrameSourcerBrownian(img_size, delay=1, step_scale=.5,
#                                     I_fluc_function=I_func_gaus,
#                                     step_fluc_function=scale_fluc,
#                                     max_count=center * 2
#                                     )

app = QtApplication()

frame_source = ArmanListener()

# set up mugglers
# (name, fill_type, #num dims)
dm = DataMuggler((('T', 'pad', 0),
                  ('img', 'bfill', 2),
                  ('count', 'bfill', 0)
                  )
                 )
dm2 = DataMuggler((('T', 'pad', 0),
                   ('max', 'bfill', 0),
                   ('x', 'bfill', 0),
                   ('y', 'bfill', 0),
                   ('count', 'bfill', 0)
                   )
                  )
# construct a watcher for the image + count on the main DataMuggler
mw = MuggleWatcherLatest(dm, 'img', ['count', 'T'])

# set up pipe line components
# multiply the image by 5 because we can
p1 = PipelineComponent(lambda msg, data: (msg,
                                          {'img': data['img'] * 5,
                                           'count': data['count'],
                                           'T': data['T']}))


def rough_center(img, axis):
    ret = np.mean(np.argmax(img, axis=axis))
    return ret

# find the max and estimate (badly) the center of the blob
p2 = PipelineComponent(lambda msg, data: (msg,
                                          {'max':
                                             np.max(data['img']),
                                          'count': data['count'],
                                          'x': rough_center(data['img'],
                                                                 axis=0),
                                          'y': rough_center(data['img'],
                                                                 axis=1),
                                          'T': data['T']
                                          }))


# hook up everything
# input
frame_source.event.connect(dm.append_data)

# first DataMuggler in to top of pipeline
mw.sig.connect(p1.sink_slot)
# p1 output -> p2 input
p1.source_signal.connect(p2.sink_slot)
# p2 output -> dm2
p2.source_signal.connect(dm2.append_data)




with enaml.imports():
    from pipeline import PipelineView

scalar_collection = ScalarCollection(data_muggler=dm2)
img_seq = DmImgSequence(data_muggler=dm, data_name='img')
cross_section_model = CrossSectionModel(data_muggler=dm, name='img',
                                        sliceable_data=img_seq)
view = PipelineView(scalar_collection=scalar_collection,
                    cross_section_model=cross_section_model)
view.show()
frame_source.start()
print('source started')
import sys
sys.exit(app.start())
