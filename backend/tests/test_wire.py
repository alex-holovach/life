import json
import cramjam
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from remote_write import encode


def test_remote_write_with_independent_protobuf_decoder():
    # A protobuf runtime independently verifies field tags, float wire types, and millisecond timestamps.
    file=descriptor_pb2.FileDescriptorProto(name="remote.proto",package="test",syntax="proto3")
    definitions={"Label":[("name",1,9,False,None),("value",2,9,False,None)],
                 "Sample":[("value",1,1,False,None),("timestamp",2,3,False,None)],
                 "TimeSeries":[("labels",1,11,True,".test.Label"),("samples",2,11,True,".test.Sample")],
                 "WriteRequest":[("timeseries",1,11,True,".test.TimeSeries")]}
    for name,fields in definitions.items():
        message=file.message_type.add(name=name)
        for name,number,kind,repeated,type_name in fields:
            field=message.field.add(name=name,number=number,type=kind,label=3 if repeated else 1)
            if type_name: field.type_name=type_name
    pool=descriptor_pool.DescriptorPool();pool.Add(file)
    request=message_factory.GetMessageClass(pool.FindMessageTypeByName("test.WriteRequest"))()
    series=json.dumps({"source":"live","device":"strap","__name__":"whoop_heart_rate_bpm"})
    request.ParseFromString(bytes(cramjam.snappy.decompress_raw(encode([
        {"series":series,"timestamp":1700000000123,"value":65.5},
        {"series":series,"timestamp":1700000000001,"value":64}]))))
    assert [(s.timestamp,s.value) for s in request.timeseries[0].samples]==[(1700000000001,64),(1700000000123,65.5)]
    assert [label.name for label in request.timeseries[0].labels]==["__name__","device","source"]
