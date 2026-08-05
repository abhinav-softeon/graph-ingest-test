package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl2;
import com.testcorp.manager.Manager2;
import com.testcorp.facade.Facade2;
import com.testcorp.service.Service22;
import com.testcorp.service.Service2;

@WebService
public class Endpoint2 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service2().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl2();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager2().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager2().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade2().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade2().orchestrateDirect(id);
    }

    @WebMethod
    public String lookupAlt1(String id) throws Exception {
        return new Service22().handle(id);
    }
}
