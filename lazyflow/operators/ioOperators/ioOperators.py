#Python
from functools import partial
import math
import logging
import glob
logger = logging.getLogger(__name__)
traceLogger = logging.getLogger('TRACE.' + __name__)

#SciPy
import vigra,numpy,h5py

#lazyflow
from lazyflow.graph import OrderedSignal, Operator, OutputSlot, InputSlot
from lazyflow.roi import roiToSlice

#deprecated #FIXME
#TODO remove
class OpH5Writer(Operator):
    name = "H5 File Writer"
    category = "Output"

    inputSlots = [InputSlot("filename", stype = "filestring"),
                  InputSlot("hdf5Path", stype = "string"), InputSlot("input"),
                  InputSlot("blockShape"),
                  InputSlot("dataType"),
                  InputSlot("roi"),
                  InputSlot("normalize")]

    outputSlots = [OutputSlot("WriteImage")]

    def setupOutputs(self):
        self.outputs["WriteImage"].meta.shape = (1,)
        self.outputs["WriteImage"].meta.dtype = object

    def execute(self, slot, subindex, roi, result):
        inputRoi = self.inputs["roi"].value
        key = roiToSlice(inputRoi[0], inputRoi[1])
        filename = self.inputs["filename"].value
        hdf5Path = self.inputs["hdf5Path"].value
        imSlot = self.inputs["input"]
        image = numpy.ndarray(imSlot.meta.shape,
                              dtype=self.inputs["dataType"].value)[key]

        #create H5File and DataSet
        f = h5py.File(filename, 'w')
        g = f

        #check for valid hdf5 path, if not valid, use default
        try:
            pathElements = hdf5Path.split("/")
            for s in pathElements[:-1]:
                g = g.create_group(s)
            d = g.create_dataset(pathElements[-1],data = image)
        except:
            print 'String {} is not a valid hdf5 path, path set to default'.format(hdf5Path)
            hdf5Path = 'volume/data'
            pathElements = hdf5Path.split("/")
            for s in pathElements[:-1]:
                g = g.create_group(s)
            d = g.create_dataset(pathElements[-1],data = image)

        #get, respectively set the blockshape as a tuple
        bs = self.inputs["blockShape"].value
        if not isinstance(bs, tuple):
            assert isinstance(bs, int)
            bs = (bs,) * len(image.shape)

        #calculate the number of blocks
        nBlockShape = numpy.array(bs)
        nshape = numpy.array(image.shape)
        blocks = numpy.ceil(nshape * 1.0 / nBlockShape).astype(numpy.int32)
        blockIndices = numpy.nonzero(numpy.ones(blocks))

        #calculate normalization
        invalue = self.inputs['input'].value
        normvalue = self.inputs['normalize'].value
        data_max, data_min = numpy.max(invalue), numpy.min(invalue)

        if normvalue == -1:
            normalize = lambda value: value
        else:
            norm_max, norm_min = normvalue

            #check normalization limits positive? ordered?
            if not (isinstance(norm_max, int) and isinstance(norm_min, int)):
                raise Exception('Normalization constants are not integers!')

            if norm_max < 0 or norm_min < 0 or norm_max < norm_min:
                norm_max,norm_min = sorted([abs(norm_max),abs(norm_min)])
                print 'WARNING: Normalization limits arent positive or ordered'

            #check combination normalization limits with datatype
            if (abs(norm_max) - abs(norm_min) <= 255 and
                (self.inputs["dataType"].value == 'uint8' or
                 self.inputs["dataType"].value == 'uint16')):
                print 'WARNING: Normalization is not appropriate for dataType'

            #normalization function
            def normalize (value):
                num = 1.0 * (value - data_min) * (norm_max - norm_min)
                denom = (data_max - data_min)
                return num / denom + norm_min

        #define write function
        def writeResult(result, blockNr, roiSlice):
            d[roiSlice] = normalize(result)
            print "writing block %d at %r" % (blockNr, roiSlice)

        requests = []

        #iter through blocks and generate requests
        print "generating block requests",
        for bnr in range(len(blockIndices[0])):
            indices = [blockIndices[0][bnr]*nBlockShape[0],]
            for i in range(1,len(nshape)):
                indices.append(blockIndices[i][bnr]*nBlockShape[i])
            nIndices = numpy.array(indices)
            start =  nIndices
            stop = numpy.minimum(nshape,start+nBlockShape)

            s = roiToSlice(start,stop)
            req = self.inputs["input"][s]
            req.notify_finished(partial(writeResult, blockNr=bnr, roiSlice=s))
            requests.append(req)
        print "... %d requests" % len(requests)

        #execute requests
        for req in requests:
            req.wait()

        f.close()
        result[0] = True
        
    def propagateDirty(self, slot, subindex, roi):
        # The output from this operator isn't generally connected to
        # other operators. If someone is using it that way, we'll
        # assume that the user wants to know that the input image has
        # become dirty and may need to be written to disk again.
        self.WriteImage.setDirty(slice(None))


class OpStackLoader(Operator):
    """Imports an image stack.

    Note: This operator does NOT cache the images, so direct access
          via the execute() function is very inefficient, especially
          through the Z-axis. Typically, you'll want to connect this
          operator to a cache whose block size is large in the X-Y
          plane.

    :param globstring: A glob string as defined by the glob module. We
        also support the following special extension to globstring
        syntax: A single string can hold a *list* of globstrings. Each
        separate globstring in the list is separated by two forward
        slashes (//). For, example,

            '/a/b/c.txt///d/e/f.txt//../g/i/h.txt'

        is parsed as

            ['/a/b/c.txt', '/d/e/f.txt', '../g/i/h.txt']

    """
    name = "Image Stack Reader"
    category = "Input"

    inputSlots = [InputSlot("globstring", stype = "string")]
    outputSlots = [OutputSlot("stack")]

    class FileOpenError( Exception ):
        def __init__(self, filename):
            self.filename = filename
            self.msg = "Unable to open file: {}".format(filename)
            super(OpStackLoader.FileOpenError, self).__init__( self.msg )

    def setupOutputs(self):
        self.fileNameList = []
        globStrings = self.inputs["globstring"].value

        # Parse list into separate globstrings and combine them
        for globString in sorted(globStrings.split("//")):
            self.fileNameList += sorted(glob.glob(globString))

        if len(self.fileNameList) != 0:
            try:
                self.info = vigra.impex.ImageInfo(self.fileNameList[0])
            except RuntimeError:
                raise OpStackLoader.FileOpenError(self.fileNameList[0])

            oslot = self.outputs["stack"]

            #build 4D shape out of 2DShape and Filelist: xyzc
            oslot.meta.shape = (self.info.getShape()[0],
                                self.info.getShape()[1],
                                len(self.fileNameList),
                                self.info.getShape()[2])
            oslot.meta.dtype = self.info.getDtype()
            zAxisInfo = vigra.AxisInfo(key='z', typeFlags=vigra.AxisType.Space)
            oslot.meta.axistags = self.info.getAxisTags()
            oslot.meta.axistags.insert(2,zAxisInfo)

        else:
            oslot = self.outputs["stack"]
            oslot.meta.shape = None
            oslot.meta.dtype = None
            oslot.meta.axistags = None

    def propagateDirty(self, slot, subindex, roi):
        assert slot == self.globstring
        # Any change to the globstring means our entire output is dirty.
        self.stack.setDirty(slice(None))

    def execute(self, slot, subindex, roi, result):
        i=0
        key = roi.toSlice()
        traceLogger.debug("OpStackLoader: Execute for: " + str(roi))
        for fileName in self.fileNameList[key[2]]:
            traceLogger.debug( "Reading image: {}".format(fileName) )
            if self.info.getShape() != vigra.impex.ImageInfo(fileName).getShape():
                raise RuntimeError('not all files have the same shape')
            # roi is in xyzc order.
            # Copy each z-slice one at a time.
            result[...,i,:] = vigra.impex.readImage(fileName)[key[0],key[1],key[3]]
            i = i+1


class OpStackWriter(Operator):
    name = "Stack File Writer"
    category = "Output"

    inputSlots = [InputSlot("filepath", stype = "string"),
                  InputSlot("dummy", stype = "list"),
                  InputSlot("input")]
    outputSlots = [OutputSlot("WritePNGStack")]

    def setupOutputs(self):
        assert self.inputs['input'].meta.getAxisKeys() == ['t', 'x', 'y', 'z', 'c']
        assert self.inputs['input'].meta.shape is not None
        self.outputs["WritePNGStack"].meta.shape = self.inputs['input'].meta.shape
        self.outputs["WritePNGStack"].meta.dtype = object

    def execute(self, slot, subindex, roi, result):
        image = self.inputs["input"][roi.toSlice()].allocate().wait()

        filepath = self.inputs["filepath"].value
        filepath = filepath.split(".")
        filetype = filepath[-1]
        filepath = filepath[0:-1]
        filepath = "/".join(filepath)
        dummy = self.inputs["dummy"].value

        if "xy" in dummy:
            pass
        if "xz" in dummy:
            pass
        if "xt" in dummy:
            for i in range(image.shape[2]):
                for j in range(image.shape[3]):
                    for k in range(image.shape[4]):
                        vigra.impex.writeImage(image[:,:,i,j,k],
                                               filepath+"-xt-y_%04d_z_%04d_c_%04d." % (i,j,k)+filetype)
        if "yz" in dummy:
            for i in range(image.shape[0]):
                for j in range(image.shape[1]):
                    for k in range(image.shape[4]):
                        vigra.impex.writeImage(image[i,j,:,:,k],
                                               filepath+"-yz-t_%04d_x_%04d_c_%04d." % (i,j,k)+filetype)
        if "yt" in dummy:
            for i in range(image.shape[1]):
                for j in range(image.shape[3]):
                    for k in range(image.shape[4]):
                        vigra.impex.writeImage(image[:,i,:,j,k],
                                               filepath+"-yt-x_%04d_z_%04d_c_%04d." % (i,j,k)+filetype)
        if "zt" in dummy:
            for i in range(image.shape[1]):
                for j in range(image.shape[2]):
                    for k in range(image.shape[4]):
                        vigra.impex.writeImage(image[:,i,j,:,k],
                                               filepath+"-zt-x_%04d_y_%04d_c_%04d." % (i,j,k)+filetype)

    def propagateDirty(self, slot, subindex, roi):
        self.WritePNGStack.setDirty(slice(None))


class OpStackToH5Writer(Operator):
    name = "OpStackToH5Writer"
    category = "IO"

    GlobString = InputSlot(stype='globstring')
    hdf5Group = InputSlot(stype='object')
    hdf5Path  = InputSlot(stype='string')

    # Requesting the output induces the copy from stack to h5 file.
    WriteImage = OutputSlot(stype='bool')

    def __init__(self, *args, **kwargs):
        super(OpStackToH5Writer, self).__init__(*args, **kwargs)
        self.progressSignal = OrderedSignal()
        self.opStackLoader = OpStackLoader(graph=self.graph, parent=self)
        self.opStackLoader.globstring.connect( self.GlobString )

    def setupOutputs(self):
        self.WriteImage.meta.shape = (1,)
        self.WriteImage.meta.dtype = object

    def propagateDirty(self, slot, subindex, roi):
        # Any change to our inputs means we're dirty
        assert slot == self.GlobString or slot == self.hdf5Group or slot == self.hdf5Path
        self.WriteImage.setDirty(slice(None))

    def execute(self, slot, subindex, roi, result):
        # Copy the data image-by-image
        stackTags = self.opStackLoader.stack.meta.axistags
        zAxis = stackTags.index('z')
        dataShape=self.opStackLoader.stack.meta.shape
        numImages = self.opStackLoader.stack.meta.shape[zAxis]

        axistags = self.opStackLoader.stack.meta.axistags
        dtype = self.opStackLoader.stack.meta.dtype
        if type(dtype) is numpy.dtype:
            # Make sure we're dealing with a type (e.g. numpy.float64),
            #  not a numpy.dtype
            dtype = dtype.type

        numChannels = dataShape[ axistags.index('c') ]

        # Set up our chunk shape: Aim for a cube that's roughly 300k in size
        dtypeBytes = dtype().nbytes
        cubeDim = math.pow( 300000 / (numChannels * dtypeBytes), (1/3.0) )
        cubeDim = int(cubeDim)

        chunkDims = {}
        chunkDims['t'] = 1
        chunkDims['x'] = cubeDim
        chunkDims['y'] = cubeDim
        chunkDims['z'] = cubeDim
        chunkDims['c'] = numChannels

        # h5py guide to chunking says chunks of 300k or less "work best"
        assert chunkDims['x'] * chunkDims['y'] * chunkDims['z'] * numChannels * dtypeBytes  <= 300000

        chunkShape = ()
        for i in range( len(dataShape) ):
            axisKey = axistags[i].key
            # Chunk shape can't be larger than the data shape
            chunkShape += ( min( chunkDims[axisKey], dataShape[i] ), )

        # Create the dataset
        internalPath = self.hdf5Path.value
        internalPath = internalPath.replace('\\', '/') # Windows fix
        group = self.hdf5Group.value
        if internalPath in group:
            del group[internalPath]

        data = group.create_dataset(internalPath,
                                    #compression='gzip',
                                    #compression_opts=4,
                                    shape=dataShape,
                                    dtype=dtype,
                                    chunks=chunkShape)
        # Now copy each image
        self.progressSignal(0)
        for z in range(numImages):
            # Ask for an entire z-slice (exactly one whole image from the stack)
            slicing = [slice(None)] * len(stackTags)
            slicing[zAxis] = slice(z, z+1)
            data[tuple(slicing)] = self.opStackLoader.stack[slicing].wait()
            self.progressSignal( z*100 / numImages )

        # We're done
        result[...] = True

        self.progressSignal(100)

        return result

if __name__ == '__main__':
    from lazyflow.graph import Graph
    import h5py
    import sys

    traceLogger.addHandler(logging.StreamHandler(sys.stdout))
    traceLogger.setLevel(logging.DEBUG)
    traceLogger.debug("HELLO")

    f = h5py.File('/tmp/flyem_sample_stack.h5')
    internalPath = 'volume/data'

    # OpStackToH5Writer
    graph = Graph()
    opStackToH5 = OpStackToH5Writer()
    opStackToH5.GlobString.setValue('/tmp/flyem_sample_stack/*.png')
    opStackToH5.hdf5Group.setValue(f)
    opStackToH5.hdf5Path.setValue(internalPath)

    success = opStackToH5.WriteImage.value
    assert success
